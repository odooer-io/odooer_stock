# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerProductBrand(models.Model):
    _name = 'odooer_stock.product.brand'
    _description = 'Product Brand'
    _order = 'name'

    name = fields.Char(string='Brand Name', required=True, translate=True)
    image = fields.Image(string='Logo')
    product_ids = fields.One2many(
        'product.template', 'brand_id', string='Products')
    product_count = fields.Integer(
        string='Products', compute='_compute_product_count')

    def _compute_product_count(self):
        for brand in self:
            brand.product_count = len(brand.product_ids)
