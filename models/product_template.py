# -*- coding: utf-8 -*-
from odoo import api, fields, models


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

    def web_name_search(self, name, specification, domain=None, operator='ilike', limit=100):
        results = super().web_name_search(name, specification, domain, operator, limit)
        use_display_name = self.env['ir.config_parameter'].sudo().get_param(
            'odooer_stock.product_dropdown_display_name'
        )
        if use_display_name:
            for r in results:
                r['__formatted_display_name'] = r.get('display_name', '')
        return results
