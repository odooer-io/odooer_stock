# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class OdooerFifoRegenerate(models.TransientModel):
    """
    Wizard to (re)build all odooer.fifo.link records from existing stock moves.

    Use this when:
    - Installing the module on an existing database (no links exist yet)
    - Correcting links after data imports or manual stock adjustments
    - Partial regeneration after a specific date (incremental update)

    Algorithm (pure SQL via PL/pgSQL function for performance):
      1. Delete existing links for outgoing moves >= from_date (or all)
      2. Load all incoming moves with their remaining qty
         (pre-date links are respected when using from_date)
      3. Walk outgoing moves in chronological order (date ASC, id ASC)
      4. For each outgoing, consume oldest available incoming moves FIFO
      5. Insert odooer.fifo.link records
    """
    _name = 'odooer.fifo.regenerate'
    _description = 'Regenerate FIFO Cost-Flow Links'

    company_id = fields.Many2one(
        'res.company', string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    from_date = fields.Date(
        string='From Date',
        help="Regenerate links only for outgoing moves on or after this date. "
             "Leave empty to regenerate all links from the beginning. "
             "Existing links for outgoing moves before this date are preserved.",
    )
    state = fields.Selection([
        ('draft', 'Ready'),
        ('done', 'Completed'),
    ], default='draft')
    link_count = fields.Integer(string='Links Created', readonly=True)
    duration_seconds = fields.Float(string='Duration (s)', readonly=True)

    def action_regenerate(self):
        """Install the SQL helper function and run bulk FIFO link generation."""
        self.ensure_one()
        import time
        t0 = time.time()

        self._install_sql_function()

        _logger.info(
            'Odooer FIFO: regenerating links for company %s from_date=%s',
            self.company_id.name, self.from_date or 'all',
        )

        self.env.cr.execute(
            "SELECT odooer_build_fifo_links(%s, %s)",
            [self.company_id.id, self.from_date or None],
        )
        link_count = self.env.cr.fetchone()[0]

        elapsed = time.time() - t0
        _logger.info(
            'Odooer FIFO: regeneration complete — %s links in %.1fs',
            link_count, elapsed,
        )

        # Invalidate caches so computed fields recompute
        self.env['stock.move'].invalidate_model(
            ['odooer_value', 'odooer_unit_cost']
        )
        self.env['product.product'].invalidate_model(['odooer_fifo_cost'])

        self.write({
            'state': 'done',
            'link_count': link_count,
            'duration_seconds': round(elapsed, 1),
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _install_sql_function(self):
        """
        Create or replace the odooer_build_fifo_links PL/pgSQL function.

        The function:
          - Deletes existing links for outgoing moves in range
          - Uses a TEMP TABLE to track remaining qty per incoming move
          - Walks outgoing moves in date+id order, consuming FIFO stack
          - Inserts odooer_fifo_link records directly
          - Returns total number of link records created
        """
        self.env.cr.execute("""
CREATE OR REPLACE FUNCTION odooer_build_fifo_links(
    p_company_id  INTEGER,
    p_from_date   DATE DEFAULT NULL
)
RETURNS INTEGER
LANGUAGE plpgsql
AS $func$
DECLARE
    v_out       RECORD;
    v_in        RECORD;
    v_remaining NUMERIC;
    v_consumed  NUMERIC;
    v_count     INTEGER := 0;
    v_uid       INTEGER;
    v_now       TIMESTAMP := NOW();
BEGIN
    -- Resolve admin uid for audit fields
    SELECT id INTO v_uid FROM res_users WHERE id = 1 LIMIT 1;

    -- ── Step 1: Delete existing links in scope ────────────────────────────
    DELETE FROM odooer_fifo_link fl
    USING stock_move sm
    WHERE fl.outgoing_move_id = sm.id
      AND sm.company_id = p_company_id
      AND (p_from_date IS NULL OR sm.date::date >= p_from_date);

    -- ── Step 2: Build temp table of incoming remaining qty ────────────────
    -- Drop and recreate to handle repeated calls within the same session
    DROP TABLE IF EXISTS _odooer_fifo_remaining;
    CREATE TEMP TABLE _odooer_fifo_remaining (
        move_id     INTEGER PRIMARY KEY,
        product_id  INTEGER NOT NULL,
        remaining   NUMERIC NOT NULL,
        move_date   TIMESTAMP NOT NULL,
        move_value  NUMERIC NOT NULL,
        move_qty    NUMERIC NOT NULL
    );

    INSERT INTO _odooer_fifo_remaining (move_id, product_id, remaining, move_date, move_value, move_qty)
    SELECT
        sm.id                                               AS move_id,
        sm.product_id,
        sm.quantity - COALESCE(pre_consumed.qty, 0.0)      AS remaining,
        sm.date                                             AS move_date,
        sm.value                                            AS move_value,
        sm.quantity                                         AS move_qty
    FROM stock_move sm
    -- Subtract quantities already consumed by outgoing moves BEFORE p_from_date
    LEFT JOIN (
        SELECT fl.incoming_move_id, SUM(fl.quantity) AS qty
        FROM odooer_fifo_link fl
        INNER JOIN stock_move out_sm ON out_sm.id = fl.outgoing_move_id
        WHERE p_from_date IS NOT NULL
          AND out_sm.date::date < p_from_date
          AND out_sm.company_id = p_company_id
        GROUP BY fl.incoming_move_id
    ) pre_consumed ON pre_consumed.incoming_move_id = sm.id
    WHERE sm.is_in      = true
      AND sm.state      = 'done'
      AND sm.company_id = p_company_id
      AND sm.quantity   > 0;

    CREATE INDEX ON _odooer_fifo_remaining (product_id, move_date, move_id);

    -- ── Step 3: Walk outgoing moves chronologically ───────────────────────
    FOR v_out IN
        SELECT sm.id AS move_id, sm.product_id, sm.quantity AS out_qty
        FROM stock_move sm
        WHERE sm.is_out      = true
          AND sm.state       = 'done'
          AND sm.company_id  = p_company_id
          AND sm.quantity    > 0
          AND (p_from_date IS NULL OR sm.date::date >= p_from_date)
        ORDER BY sm.date ASC, sm.id ASC
    LOOP
        v_remaining := v_out.out_qty;

        -- Consume oldest incoming moves for this product (FIFO order)
        FOR v_in IN
            SELECT move_id, remaining
            FROM _odooer_fifo_remaining
            WHERE product_id = v_out.product_id
              AND remaining  > 0.0000001
            ORDER BY move_date ASC, move_id ASC
        LOOP
            EXIT WHEN v_remaining <= 0.0000001;

            v_consumed := LEAST(v_in.remaining, v_remaining);

            INSERT INTO odooer_fifo_link (
                incoming_move_id, outgoing_move_id, quantity,
                create_uid, create_date, write_uid, write_date
            ) VALUES (
                v_in.move_id, v_out.move_id, v_consumed,
                v_uid, v_now, v_uid, v_now
            );

            UPDATE _odooer_fifo_remaining
               SET remaining = remaining - v_consumed
             WHERE move_id   = v_in.move_id;

            v_remaining := v_remaining - v_consumed;
            v_count     := v_count + 1;
        END LOOP;
    END LOOP;

    -- ── Step 4: Cleanup ───────────────────────────────────────────────────
    DROP TABLE IF EXISTS _odooer_fifo_remaining;

    RETURN v_count;
END;
$func$;
        """)
