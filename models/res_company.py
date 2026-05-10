# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    odooer_fifo_enabled = fields.Boolean(
        string='Enable FIFO Cost-Flow Tracking (Odooer)',
        compute='_compute_odooer_fifo_enabled',
        help="Computed from the Odooer FIFO group membership (instance-wide setting).",
    )

    @api.depends_context('uid')
    def _compute_odooer_fifo_enabled(self):
        group = self.env.ref('odooer_stock.group_odooer_fifo_enabled', raise_if_not_found=False)
        base_user_group = self.env.ref('base.group_user', raise_if_not_found=False)
        enabled = bool(group and base_user_group and group in base_user_group.sudo().implied_ids)
        for company in self:
            company.odooer_fifo_enabled = enabled
