# -*- coding: utf-8 -*-
from odoo import api, fields, models


class OdooerFifoLink(models.Model):
    """
    Links one incoming stock.move to one outgoing stock.move with a consumed
    quantity, establishing the FIFO cost-flow chain.

    - quantity is fixed at the time the outgoing move is done.
    - unit_cost and outgoing_value are dynamic: they cascade from changes to
      incoming_move_id.value (bills, landed costs, manual adjustments).
    """
    _name = 'odooer.fifo.link'
    _description = 'FIFO Cost-Flow Link (Incoming → Outgoing)'
    _order = 'outgoing_move_id, id'

    incoming_move_id = fields.Many2one(
        'stock.move', string='Incoming Move',
        required=True, index=True, ondelete='restrict',
    )
    outgoing_move_id = fields.Many2one(
        'stock.move', string='Outgoing Move',
        required=True, index=True, ondelete='restrict',
    )
    quantity = fields.Float(
        string='Consumed Quantity',
        digits='Product Unit of Measure',
        help="Quantity of the incoming move consumed by the outgoing move. "
             "Fixed at the time the outgoing move is validated.",
    )

    # ── Computed fields (dynamic — cascade from incoming move value) ──────────

    unit_cost = fields.Float(
        string='Unit Cost',
        compute='_compute_outgoing_value', store=True,
        digits='Product Price',
        help="Unit cost of the incoming move at the time of computation. "
             "Updates automatically when the incoming move's value changes.",
    )
    outgoing_value = fields.Float(
        string='Outgoing Value',
        compute='_compute_outgoing_value', store=True,
        digits='Account',
        help="FIFO-attributed value for this quantity: quantity × unit_cost.",
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='incoming_move_id.company_id.currency_id',
        string='Currency',
    )

    # ── Related convenience fields ────────────────────────────────────────────

    incoming_date = fields.Datetime(
        related='incoming_move_id.date', string='Receipt Date', store=True,
    )
    outgoing_date = fields.Datetime(
        related='outgoing_move_id.date', string='Delivery Date', store=True,
    )
    product_id = fields.Many2one(
        related='outgoing_move_id.product_id', string='Product', store=True,
    )
    company_id = fields.Many2one(
        related='outgoing_move_id.company_id', string='Company', store=True,
    )

    # ── Compute ───────────────────────────────────────────────────────────────

    @api.depends('quantity', 'incoming_move_id.value', 'incoming_move_id.quantity')
    def _compute_outgoing_value(self):
        for link in self:
            incoming = link.incoming_move_id
            if incoming.quantity:
                link.unit_cost = incoming.value / incoming.quantity
            else:
                link.unit_cost = 0.0
            link.outgoing_value = link.quantity * link.unit_cost
