# -*- coding: utf-8 -*-
from odoo import api, fields, models


class OdooerStockMoveUpdateValue(models.TransientModel):
    """Wizard to manually override a stock.move's valuation `value`.

    Mirrors the inline-editable `value` field already available on
    stock.view_move_tree (stock_account), but exposed as a confirm dialog
    for use from the move's form view.
    """
    _name = 'odooer.stock.move.update.value'
    _description = 'Update Move Value'

    move_id = fields.Many2one(
        'stock.move', string='Stock Move', required=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='move_id.company_currency_id', string='Currency',
    )
    value = fields.Monetary(
        string='Value', currency_field='currency_id', required=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'move_id' in res:
            move = self.env['stock.move'].browse(res['move_id'])
            res.setdefault('value', move.value)
        return res

    def action_confirm(self):
        self.ensure_one()
        self.move_id.value = self.value
        return {'type': 'ir.actions.act_window_close'}
