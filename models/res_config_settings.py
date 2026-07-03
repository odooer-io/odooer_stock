# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    group_odooer_fifo_enabled = fields.Boolean(
        string='Enable FIFO Cost-Flow Tracking (Odooer)',
        implied_group='odooer_stock.group_odooer_fifo_enabled',
    )
    odooer_product_dropdown_display_name = fields.Boolean(
        string='Use Standard Display Name in Product Dropdown',
        help='When enabled, the product dropdown shows [Ref] Name format instead of the tab-separated variant.',
        config_parameter='odooer_stock.product_dropdown_display_name',
    )
