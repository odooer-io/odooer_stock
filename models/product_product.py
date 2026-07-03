# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    odooer_fifo_cost = fields.Float(
        string='Current FIFO Cost (Odooer)',
        compute='_compute_odooer_fifo_cost', store=True,
        digits='Product Price',
        help="True FIFO next-out unit cost: unit cost of the oldest incoming "
             "move that still has unconsumed quantity according to the FIFO "
             "cost-flow links. Updated whenever any FIFO link is created or "
             "the incoming move's value changes.",
    )

    @api.depends(
        'stock_move_ids.odooer_incoming_link_ids.quantity',
        'stock_move_ids.value',
        'stock_move_ids.quantity',
        'stock_move_ids.state',
        'stock_move_ids.is_in',
    )
    def _compute_odooer_fifo_cost(self):
        for product in self:
            # Find all done incoming moves for this product
            incoming_moves = self.env['stock.move'].search([
                ('product_id', '=', product.id),
                ('is_in', '=', True),
                ('state', '=', 'done'),
                ('quantity', '>', 0),
            ], order='date asc, id asc')

            fifo_cost = 0.0
            for move in incoming_moves:
                consumed = sum(
                    move.odooer_incoming_link_ids.mapped('quantity')
                )
                remaining = move.quantity - consumed
                if product.uom_id.compare(remaining, 0) > 0:
                    fifo_cost = move.value / move.quantity if move.quantity else 0.0
                    break  # oldest move with remaining stock found

            product.odooer_fifo_cost = fifo_cost

    @api.model
    @api.readonly
    def web_name_search(self, name, specification, domain=None, operator='ilike', limit=100):
        results = super().web_name_search(name, specification, domain, operator, limit)
        use_display_name = self.env['ir.config_parameter'].sudo().get_param(
            'odooer_stock.product_dropdown_display_name'
        )
        if use_display_name:
            for r in results:
                r['__formatted_display_name'] = r.get('display_name', '')
        return results
