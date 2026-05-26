# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerProductGroup(models.Model):
    _name = 'odooer_stock.product.group'
    _description = 'Product Group'
    _order = 'name'

    name = fields.Char(string='Group Name', required=True, translate=True)
    product_ids = fields.One2many(
        'product.template', 'group_id', string='Products')
    product_count = fields.Integer(
        string='Products', compute='_compute_product_count')

    def _compute_product_count(self):
        for group in self:
            group.product_count = len(group.product_ids)
