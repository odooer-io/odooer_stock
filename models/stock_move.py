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

    # ── Compute ───────────────────────────────────────────────────────────────

    @api.depends('odooer_fifo_link_ids.outgoing_value', 'quantity')
    def _compute_odooer_value(self):
        for move in self:
            total = sum(move.odooer_fifo_link_ids.mapped('outgoing_value'))
            move.odooer_value = total
            move.odooer_unit_cost = total / move.quantity if move.quantity else 0.0

    # ── _action_done hook ─────────────────────────────────────────────────────

    def _action_done(self, cancel_backorder=False):
        # Capture outgoing moves BEFORE super() so we still have access to
        # the FIFO stack in its current state (same pattern as Odoo's own
        # _set_value call order).
        moves_out = self.filtered(lambda m: m._is_out())

        result = super()._action_done(cancel_backorder=cancel_backorder)

        # Generate FIFO links for companies that have the feature enabled.
        done_out = moves_out.exists().filtered(
            lambda m: m.state == 'done'
            and m.company_id.odooer_fifo_enabled
        )
        if done_out:
            done_out._create_odooer_fifo_links()

        return result

    def _create_odooer_fifo_links(self):
        """
        Walk our own FIFO stack to create odooer.fifo.link records for each
        outgoing move.

        We intentionally do NOT use product._run_fifo_get_stack() because that
        method uses qty_available, which has already been decremented by the
        time _action_done returns.  Instead we query our own odooer_fifo_link
        table to compute remaining qty per incoming move.

        Because we call FifoLink.create() inside the loop, each subsequent
        outgoing move in the same batch sees the updated remaining quantities
        from previously created links — correct FIFO ordering within a batch.

        Uses sudo() so that inventory users without admin rights can trigger
        this when validating a delivery.
        """
        FifoLink = self.env['odooer.fifo.link'].sudo()
        cr = self.env.cr

        for move in self:
            product = move.product_id
            qty_to_consume = move._get_valued_qty()
            if product.uom_id.compare(qty_to_consume, 0) <= 0:
                continue

            # Incoming moves with remaining qty, oldest first (FIFO order).
            # Remaining = move_line qty  -  already consumed via our links.
            cr.execute("""
                SELECT sm.id,
                       COALESCE(sml.qty, 0) - COALESCE(consumed.qty, 0) AS available
                FROM stock_move sm
                LEFT JOIN (
                    SELECT move_id, SUM(quantity_product_uom) AS qty
                    FROM stock_move_line
                    WHERE state = 'done'
                    GROUP BY move_id
                ) sml ON sml.move_id = sm.id
                LEFT JOIN (
                    SELECT incoming_move_id, SUM(quantity) AS qty
                    FROM odooer_fifo_link
                    GROUP BY incoming_move_id
                ) consumed ON consumed.incoming_move_id = sm.id
                WHERE sm.product_id = %s
                  AND sm.is_in = TRUE
                  AND sm.state = 'done'
                  AND sm.company_id = %s
                  AND COALESCE(sml.qty, 0) - COALESCE(consumed.qty, 0) > 0
                ORDER BY sm.date ASC, sm.id ASC
            """, (product.id, move.company_id.id))

            remaining = qty_to_consume
            links_to_create = []

            for (in_move_id, available) in cr.fetchall():
                if remaining <= 0:
                    break
                consumed = min(available, remaining)
                links_to_create.append({
                    'incoming_move_id': in_move_id,
                    'outgoing_move_id': move.id,
                    'quantity': consumed,
                })
                remaining -= consumed

            if links_to_create:
                FifoLink.create(links_to_create)
