# -*- coding: utf-8 -*-
from odoo import api, fields, models
from collections import defaultdict
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
        Walk the FIFO stack for each outgoing move and create
        odooer.fifo.link records.  Mirrors the algorithm in
        product._run_fifo() but stores links instead of computing a value.
        """
        FifoLink = self.env['odooer.fifo.link']
        # Track how much of each product's FIFO qty is already consumed
        # when multiple outgoing moves are done simultaneously.
        fifo_qty_processed = defaultdict(float)

        for move in self:
            product = move.product_id
            qty_to_consume = move._get_valued_qty()
            if product.uom_id.compare(qty_to_consume, 0) <= 0:
                continue

            fifo_stack, qty_on_first_move = product.with_context(
                fifo_qty_already_processed=fifo_qty_processed[product]
            )._run_fifo_get_stack()

            remaining = qty_to_consume
            links_to_create = []

            while remaining > 0 and fifo_stack:
                incoming = fifo_stack.pop(0)
                valued_qty = incoming._get_valued_qty()

                if qty_on_first_move:
                    # The first move in the stack may only be partially available
                    available = qty_on_first_move
                    qty_on_first_move = 0
                else:
                    available = valued_qty

                consumed = min(available, remaining)
                links_to_create.append({
                    'incoming_move_id': incoming.id,
                    'outgoing_move_id': move.id,
                    'quantity': consumed,
                })
                remaining -= consumed

            if links_to_create:
                FifoLink.create(links_to_create)

            fifo_qty_processed[product] += qty_to_consume
