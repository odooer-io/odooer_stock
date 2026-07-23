# -*- coding: utf-8 -*-
from odoo import api, fields, models
import logging

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    # ── FIFO link fields ──────────────────────────────────────────────────────

    odooer_fifo_link_ids = fields.One2many(
        'odooer.fifo.link', 'outgoing_move_id',
        string='FIFO Cost Links',
        help="FIFO cost-flow links attributed to this outgoing move.",
    )
    odooer_incoming_link_ids = fields.One2many(
        'odooer.fifo.link', 'incoming_move_id',
        string='Consumed by (FIFO Links)',
    )

    # Remaining qty in the FIFO queue for incoming moves.
    # Set when the move is validated; decremented as outgoing moves consume it.
    # NULL means "not tracked" (outgoing moves, moves before module install).
    # Maintained manually via raw SQL for performance.
    odooer_remaining_qty = fields.Float(
        string='FIFO Remaining Qty',
        digits='Product Unit of Measure',
        readonly=True,
        help="Remaining quantity available in the FIFO stack for this incoming move.",
    )

    odooer_value = fields.Monetary(
        string='FIFO Value (Odooer)',
        compute='_compute_odooer_value', store=True,
        currency_field='company_currency_id',
        help="FIFO-attributed COGS for this outgoing move. "
             "Automatically updates when any linked incoming move's value "
             "changes (bill, landed cost, manual adjustment).",
    )
    odooer_unit_cost = fields.Float(
        string='FIFO Unit Cost (Odooer)',
        compute='_compute_odooer_value', store=True,
        digits='Product Price',
        help="Effective unit cost = odooer_value / quantity.",
    )

    signed_odooer_value = fields.Monetary(
        string='Signed Value',
        compute='_compute_signed_odooer_value', store=True,
        currency_field='company_currency_id',
        help="Move value signed by direction: negative for outgoing moves, "
             "positive for incoming moves. Useful for running totals that "
             "combine both directions (e.g. delivered COGS vs. received value).",
    )

    # ── Partial index for fast FIFO queue lookup ──────────────────────────────

    def init(self):
        # Partial index: only the small set of incoming moves still in the
        # FIFO queue (odooer_remaining_qty > 0). Keeps the FIFO query O(1).
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS odooer_stock_move_fifo_queue_idx
            ON stock_move (product_id, company_id, date, id)
            WHERE odooer_remaining_qty > 0
        """)

    # ── Compute ───────────────────────────────────────────────────────────────

    @api.depends('odooer_fifo_link_ids.outgoing_value', 'quantity')
    def _compute_odooer_value(self):
        for move in self:
            total = sum(move.odooer_fifo_link_ids.mapped('outgoing_value'))
            move.odooer_value = total
            move.odooer_unit_cost = total / move.quantity if move.quantity else 0.0

    @api.depends('odooer_value', 'is_in', 'is_out')
    def _compute_signed_odooer_value(self):
        for move in self:
            move.signed_odooer_value = -move.odooer_value if move.is_out else move.odooer_value

    # ── Update Value wizard ───────────────────────────────────────────────────

    def action_open_update_value_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Update Value',
            'res_model': 'odooer.stock.move.update.value',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_move_id': self.id},
        }

    # ── _action_done hook ─────────────────────────────────────────────────────

    def _action_done(self, cancel_backorder=False):
        moves_in = self.filtered(lambda m: m._is_in())
        moves_out = self.filtered(lambda m: m._is_out())

        result = super()._action_done(cancel_backorder=cancel_backorder)

        # Single instance-wide check (odooer_fifo_enabled is group-based)
        if not self.env.company.sudo().odooer_fifo_enabled:
            return result

        done_in = moves_in.exists().filtered(lambda m: m.state == 'done')
        if done_in:
            done_in._init_odooer_remaining_qty()

        done_out = moves_out.exists().filtered(lambda m: m.state == 'done')
        if done_out:
            done_out._create_odooer_fifo_links()

        return result

    def _init_odooer_remaining_qty(self):
        """Set odooer_remaining_qty for newly validated incoming moves.

        Also performs retroactive FIFO correction: if a new incoming arrives
        with a date/id that places it earlier in the FIFO queue than incomings
        already consumed by existing outgoing moves, those outgoing links are
        invalidated and rebuilt in correct FIFO order.  This prevents the
        "full regeneration" from being needed just to fix ordering violations
        caused by backdated or late-arrived receipts.
        """
        cr = self.env.cr

        # Step 1: Set odooer_remaining_qty for all new incomings in one batch
        moves_with_qty = []
        qty_updates = []
        for move in self:
            qty = move._get_valued_qty()
            if qty > 0:
                moves_with_qty.append(move)
                qty_updates.append((qty, move.id))

        if qty_updates:
            cr.executemany(
                "UPDATE stock_move SET odooer_remaining_qty = %s WHERE id = %s",
                qty_updates,
            )
        self.invalidate_recordset(['odooer_remaining_qty'])

        if not moves_with_qty:
            return

        # Step 2: Single batched query — join all new incomings as a VALUES
        # table so we only hit the DB once regardless of receipt size.
        # A violation exists when an outgoing move (date >= new incoming's date)
        # consumed from an incoming NEWER than the new one, or had an overflow
        # link (incoming_move_id IS NULL).
        rows_sql = ', '.join(
            ['(%s::int, %s::int, %s::timestamp, %s::int)'] * len(moves_with_qty)
        )
        params = []
        for m in moves_with_qty:
            params.extend([m.product_id.id, m.company_id.id, m.date, m.id])

        cr.execute(f"""
            SELECT DISTINCT fl.outgoing_move_id
            FROM   odooer_fifo_link fl
            JOIN   stock_move out_sm ON out_sm.id = fl.outgoing_move_id
            LEFT JOIN stock_move in_sm ON in_sm.id = fl.incoming_move_id
            JOIN (VALUES {rows_sql}) AS new_in(product_id, company_id, in_date, in_id)
              ON  out_sm.product_id = new_in.product_id
             AND  out_sm.company_id = new_in.company_id
             AND  (out_sm.date, out_sm.id) >= (new_in.in_date, new_in.in_id)
             AND  (
                    fl.incoming_move_id IS NULL                       -- overflow
                    OR (in_sm.date, in_sm.id) > (new_in.in_date, new_in.in_id)
                  )
        """, params)
        affected_out_ids = {row[0] for row in cr.fetchall()}

        if not affected_out_ids:
            return

        _logger.info(
            'Odooer FIFO: retroactive re-link triggered for %d outgoing moves '
            '(late-arriving incoming move(s): %s)',
            len(affected_out_ids),
            [m.id for m in moves_with_qty],
        )

        out_ids_list = list(affected_out_ids)

        # Step 3: Restore odooer_remaining_qty on incomings whose links are
        # about to be deleted, so the FIFO queue is correct for re-linking.
        cr.execute("""
            UPDATE stock_move sm
            SET    odooer_remaining_qty = odooer_remaining_qty + sub.qty
            FROM   (
                SELECT fl.incoming_move_id, SUM(fl.quantity) AS qty
                FROM   odooer_fifo_link fl
                WHERE  fl.outgoing_move_id = ANY(%s)
                  AND  fl.incoming_move_id IS NOT NULL
                GROUP BY fl.incoming_move_id
            ) sub
            WHERE sm.id = sub.incoming_move_id
        """, (out_ids_list,))

        # Step 4: Delete the now-stale links
        cr.execute(
            "DELETE FROM odooer_fifo_link WHERE outgoing_move_id = ANY(%s)",
            (out_ids_list,),
        )
        self.env['stock.move'].invalidate_model(['odooer_remaining_qty'])

        # Step 5: Re-create links for affected outgoing moves in strict FIFO
        # order (oldest outgoing first) so the queue is consumed correctly.
        affected_moves = self.env['stock.move'].browse(out_ids_list).sorted(
            key=lambda m: (m.date, m.id)
        )
        affected_moves._create_odooer_fifo_links()

    def _create_odooer_fifo_links(self):
        """
        Walk the FIFO queue to create odooer.fifo.link records for each
        outgoing move.

        Uses odooer_remaining_qty on stock_move (maintained incrementally)
        instead of aggregating the full link table — only the small open
        queue is scanned. A partial index on (product_id, company_id, date, id)
        WHERE odooer_remaining_qty > 0 makes this query very fast.

        Overflow / negative stock: when the FIFO stack is exhausted before
        the outgoing qty is fully consumed, an overflow link is created with
        incoming_move_id=NULL and override_unit_cost set to the last consumed
        incoming move's unit cost (or product.standard_price if the stack was
        completely empty). This matches Odoo's own _run_fifo() behaviour.

        Uses sudo() so that inventory users without admin rights can trigger
        this when validating a delivery.
        """
        FifoLink = self.env['odooer.fifo.link'].sudo()
        cr = self.env.cr

        for move in self:
            qty_to_consume = move._get_valued_qty()
            if move.product_id.uom_id.compare(qty_to_consume, 0) <= 0:
                continue

            cr.execute("""
                SELECT id, odooer_remaining_qty,
                       CASE WHEN quantity > 0 THEN value / quantity ELSE 0 END AS unit_cost
                FROM stock_move
                WHERE product_id = %s
                  AND company_id = %s
                  AND odooer_remaining_qty > 0
                ORDER BY date ASC, id ASC
            """, (move.product_id.id, move.company_id.id))

            remaining = qty_to_consume
            links_to_create = []
            decrements = []
            last_unit_cost = None

            for (in_move_id, available, unit_cost) in cr.fetchall():
                if remaining <= 0:
                    break
                consumed = min(available, remaining)
                links_to_create.append({
                    'incoming_move_id': in_move_id,
                    'outgoing_move_id': move.id,
                    'quantity': consumed,
                })
                decrements.append((consumed, in_move_id))
                last_unit_cost = unit_cost
                remaining -= consumed

            # Overflow: outgoing exceeds available FIFO stack
            if remaining > 0:
                if last_unit_cost is None:
                    last_unit_cost = move.product_id.standard_price
                links_to_create.append({
                    'incoming_move_id': False,
                    'outgoing_move_id': move.id,
                    'quantity': remaining,
                    'override_unit_cost': last_unit_cost,
                })

            if links_to_create:
                FifoLink.create(links_to_create)
                if decrements:
                    cr.executemany(
                        "UPDATE stock_move SET odooer_remaining_qty = odooer_remaining_qty - %s WHERE id = %s",
                        decrements,
                    )
                self.env['stock.move'].invalidate_model(['odooer_remaining_qty'])
