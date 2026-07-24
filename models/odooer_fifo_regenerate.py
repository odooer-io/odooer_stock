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
    from_date = fields.Date(string='From Date')  # kept for PL/pgSQL compat; always None
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
            "SELECT odooer_build_fifo_links(%s, NULL)",
            [self.company_id.id],
        )
        link_count = self.env.cr.fetchone()[0]

        elapsed = time.time() - t0
        _logger.info(
            'Odooer FIFO: regeneration complete — %s links in %.1fs',
            link_count, elapsed,
        )

        self._recompute_downstream_fields()

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

    def _recompute_downstream_fields(self):
        """
        Force-recompute and persist every stored computed field that
        depends on odooer_fifo_link, after it was bulk-rebuilt via raw SQL.

        Raw SQL INSERT/DELETE on odooer_fifo_link bypasses the ORM, so it
        never triggers the automatic @api.depends recompute machinery.
        invalidate_model() alone is NOT enough for stored fields — it only
        clears the in-memory cache; the stale value stays in the DB column
        until the field is explicitly recomputed and flushed. This caused
        stock_move.odooer_value (and everything derived from it) to drift
        out of sync with the freshly-rebuilt odooer_fifo_link records.
        """
        StockMove = self.env['stock.move']
        FifoLink = self.env['odooer.fifo.link']

        # Invalidate the reverse one2many caches so we re-read fresh links
        # from the DB (the SQL function fully replaced them).
        StockMove.invalidate_model([
            'odooer_fifo_link_ids', 'odooer_incoming_link_ids',
            'odooer_remaining_qty',  # plain field, written directly by SQL
        ])

        # 1) odooer.fifo.link: unit_cost / outgoing_value.
        # The SQL function already computes/inserts matching values directly,
        # this is a belt-and-braces consistency pass so the ORM-side compute
        # formula and the stored DB values can never silently diverge.
        links = FifoLink.search([('company_id', '=', self.company_id.id)])
        if links:
            links._compute_outgoing_value()
            links.flush_recordset(['unit_cost', 'outgoing_value'])

        # 2) stock.move: odooer_value / odooer_unit_cost (FIFO COGS,
        # outgoing moves only) and signed_odooer_value (both directions).
        moves = StockMove.search([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'done'),
            '|', ('is_in', '=', True), ('is_out', '=', True),
        ])
        if moves:
            moves._compute_odooer_value()
            moves.flush_recordset(['odooer_value', 'odooer_unit_cost'])
            moves._compute_signed_odooer_value()
            moves.flush_recordset(['signed_odooer_value'])

        # 3) product.product: odooer_fifo_cost (current FIFO next-out cost).
        products = self.env['product.product'].search([
            ('id', 'in', moves.mapped('product_id.id')),
        ])
        if products:
            products._compute_odooer_fifo_cost()
            products.flush_recordset(['odooer_fifo_cost'])

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
    v_out           RECORD;
    v_in            RECORD;
    v_remaining     NUMERIC;
    v_consumed      NUMERIC;
    v_count         INTEGER := 0;
    v_uid           INTEGER;
    v_now           TIMESTAMP := NOW();
    v_last_unit_cost NUMERIC;
    v_std_price     NUMERIC;
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
        sm.id                                                                   AS move_id,
        sm.product_id,
        -- Use quantity_product_uom from move lines (already in product's default UOM,
        -- rounded to UOM precision — matches Odoo's own FIFO stack calculation)
        COALESCE(sml_in.qty, 0)
            - COALESCE(pre_consumed.qty, 0.0)                                  AS remaining,
        sm.date                                                                 AS move_date,
        sm.value                                                                AS move_value,
        COALESCE(sml_in.qty, 0)                                                AS move_qty
    FROM stock_move sm
    LEFT JOIN (
        SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
        FROM stock_move_line sml
        WHERE sml.state = 'done'
        GROUP BY sml.move_id
    ) sml_in ON sml_in.move_id = sm.id
    -- Subtract quantities already consumed by outgoing moves BEFORE p_from_date
    -- (pre-existing links are already stored in product default UOM)
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
      AND COALESCE(sml_in.qty, 0) > 0;

    CREATE INDEX ON _odooer_fifo_remaining (product_id, move_date, move_id);

    -- ── Step 3: Walk outgoing moves chronologically ───────────────────────
    FOR v_out IN
        SELECT sm.id AS move_id, sm.product_id,
               -- Use quantity_product_uom from move lines (rounded, matches Odoo's FIFO)
               COALESCE(sml_out.qty, 0) AS out_qty,
               sm.date AS move_date
        FROM stock_move sm
        LEFT JOIN (
            SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
            FROM stock_move_line sml
            WHERE sml.state = 'done'
            GROUP BY sml.move_id
        ) sml_out ON sml_out.move_id = sm.id
        WHERE sm.is_out      = true
          AND sm.state       = 'done'
          AND sm.company_id  = p_company_id
          AND COALESCE(sml_out.qty, 0) > 0
          AND (p_from_date IS NULL OR sm.date::date >= p_from_date)
        ORDER BY sm.date ASC, sm.id ASC
    LOOP
        v_remaining := v_out.out_qty;
        v_last_unit_cost := NULL;

        -- Consume oldest incoming moves for this product (FIFO order)
        FOR v_in IN
            SELECT move_id, remaining, move_value, move_qty, move_date
            FROM _odooer_fifo_remaining
            WHERE product_id = v_out.product_id
              AND remaining  > 0.0000001
            ORDER BY move_date ASC, move_id ASC
        LOOP
            EXIT WHEN v_remaining <= 0.0000001;

            v_consumed := LEAST(v_in.remaining, v_remaining);
            v_last_unit_cost := CASE WHEN v_in.move_qty > 0 THEN v_in.move_value / v_in.move_qty ELSE 0 END;

            INSERT INTO odooer_fifo_link (
                incoming_move_id, outgoing_move_id, quantity,
                company_id, product_id,
                incoming_date, outgoing_date,
                unit_cost, outgoing_value,
                create_uid, create_date, write_uid, write_date
            ) VALUES (
                v_in.move_id, v_out.move_id, v_consumed,
                p_company_id, v_out.product_id,
                v_in.move_date, v_out.move_date,
                v_last_unit_cost,
                v_consumed * v_last_unit_cost,
                v_uid, v_now, v_uid, v_now
            );

            UPDATE _odooer_fifo_remaining
               SET remaining = remaining - v_consumed
             WHERE move_id   = v_in.move_id;

            v_remaining := v_remaining - v_consumed;
            v_count     := v_count + 1;
        END LOOP;

        -- Overflow: outgoing qty exceeds FIFO stack — use last known cost
        -- (or standard_price if the stack was completely empty)
        IF v_remaining > 0.0000001 THEN
            IF v_last_unit_cost IS NULL THEN
                SELECT COALESCE((standard_price->>(p_company_id::text))::numeric, 0) INTO v_std_price
                FROM product_template pt
                JOIN product_product pp ON pp.product_tmpl_id = pt.id
                WHERE pp.id = v_out.product_id
                LIMIT 1;
                v_last_unit_cost := v_std_price;
            END IF;

            INSERT INTO odooer_fifo_link (
                incoming_move_id, outgoing_move_id, quantity,
                company_id, product_id,
                incoming_date, outgoing_date,
                unit_cost, outgoing_value, override_unit_cost,
                create_uid, create_date, write_uid, write_date
            ) VALUES (
                NULL, v_out.move_id, v_remaining,
                p_company_id, v_out.product_id,
                NULL, v_out.move_date,
                v_last_unit_cost,
                v_remaining * v_last_unit_cost,
                v_last_unit_cost,
                v_uid, v_now, v_uid, v_now
            );
            v_count := v_count + 1;
        END IF;
    END LOOP;

    -- ── Step 4: Update odooer_remaining_qty and cleanup ──────────────────
    -- Single LEFT JOIN pass: moves in the temp table get their actual
    -- remaining qty; moves NOT in the temp table (fully consumed or never
    -- had remaining) are zeroed out.
    -- Scoped to the regen range so partial reruns don't touch older moves.
    UPDATE stock_move sm
       SET odooer_remaining_qty = COALESCE(GREATEST(upd.remaining, 0), 0)
      FROM (
          SELECT sm2.id, fr.remaining
          FROM stock_move sm2
          LEFT JOIN _odooer_fifo_remaining fr ON fr.move_id = sm2.id
          WHERE sm2.is_in      = TRUE
            AND sm2.state      = 'done'
            AND sm2.company_id = p_company_id
            AND (p_from_date IS NULL OR sm2.date::date >= p_from_date)
      ) upd
     WHERE sm.id = upd.id;

    DROP TABLE IF EXISTS _odooer_fifo_remaining;

    RETURN v_count;
END;
$func$;
        """)
