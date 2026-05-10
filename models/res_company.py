# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    odooer_fifo_enabled = fields.Boolean(
        string='Enable FIFO Cost-Flow Tracking (Odooer)',
        default=False,
        help="When enabled, every outgoing stock move creates FIFO cost-flow "
             "link records (odooer.fifo.link) that match it to the specific "
             "incoming receipts it consumes. This powers the FIFO value, "
             "historical inventory valuation, and the sale gross profit report. "
             "Disable to skip link generation (no performance impact).",
    )
