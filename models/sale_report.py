# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleReport(models.Model):
    _inherit = 'sale.report'

    brand_id = fields.Many2one(
        comodel_name='odooer_stock.product.brand',
        string='Product Brand',
        readonly=True,
    )
    group_id = fields.Many2one(
        comodel_name='odooer_stock.product.group',
        string='Product Group',
        readonly=True,
    )

    def _select_additional_fields(self):
        res = super()._select_additional_fields()
        res['brand_id'] = 't.brand_id'
        res['group_id'] = 't.group_id'
        return res

    def _group_by_sale(self):
        return super()._group_by_sale() + ',\n            t.brand_id,\n            t.group_id'
