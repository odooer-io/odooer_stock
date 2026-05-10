# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    odooer_fifo_enabled = fields.Boolean(
        related='company_id.odooer_fifo_enabled',
        readonly=False,
        string='Enable FIFO Cost-Flow Tracking (Odooer)',
        implied_group='odooer_stock.group_odooer_fifo_enabled',
    )
