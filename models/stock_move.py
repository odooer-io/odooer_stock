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
        """Set odooer_remaining_qty for newly validated incoming moves."""
        cr = self.env.cr
        for move in self:
            qty = move._get_valued_qty()
            if qty > 0:
                cr.execute(
                    "UPDATE stock_move SET odooer_remaining_qty = %s WHERE id = %s",
                    (qty, move.id),
                )
        self.invalidate_recordset(['odooer_remaining_qty'])

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
