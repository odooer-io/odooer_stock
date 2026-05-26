# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    brand_id = fields.Many2one(
        'odooer_stock.product.brand',
        string='Brand',
        index=True,
    )
    group_id = fields.Many2one(
        'odooer_stock.product.group',
        string='Product Group',
        index=True,
    )
