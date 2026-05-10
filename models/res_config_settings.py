# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    group_odooer_fifo_enabled = fields.Boolean(
        string='Enable FIFO Cost-Flow Tracking (Odooer)',
        implied_group='odooer_stock.group_odooer_fifo_enabled',
    )
