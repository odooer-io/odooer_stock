# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerOutgoingReport(models.Model):
    """
    Outgoing stock analysis report.

    One row per odooer.fifo.link record — shows every outgoing move with
    its FIFO-attributed cost and value.  Because unit_cost and outgoing_value
    are stored on odooer.fifo.link and update dynamically when incoming move
    values change (bills, landed costs), this report always reflects the
    latest settled costs.

    No date parameter needed — filter by outgoing_date or incoming_date
    using the standard search bar.
    """
    _name = 'odooer.outgoing.report'
    _description = 'Odooer Outgoing Stock Analysis'
    _auto = False
    _order = 'outgoing_date desc, id'

    # ── Dimensions ────────────────────────────────────────────────────────────
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner', readonly=True)
    picking_id = fields.Many2one('stock.picking', string='Picking', readonly=True)
    reference = fields.Char(string='Reference', readonly=True)
    outgoing_date = fields.Date(string='Outgoing Date', readonly=True)
    incoming_date = fields.Date(string='Incoming Date', readonly=True)
    outgoing_usage = fields.Selection(
        selection=[
            ('sale', 'Sale / Delivery'),
            ('purchase_return', 'Purchase Return'),
            ('scrap', 'Scrap'),
            ('inventory', 'Inventory Adjustment'),
            ('internal', 'Internal Transfer'),
            ('other', 'Other'),
        ],
        string='Usage',
        readonly=True,
    )

    # ── Measures ──────────────────────────────────────────────────────────────
    quantity = fields.Float(string='Qty', digits='Product Unit of Measure', readonly=True)
    unit_cost = fields.Float(string='Unit Cost', digits='Product Price', readonly=True)
    outgoing_value = fields.Float(string='Outgoing Value', digits='Product Price', readonly=True)

    # ── SQL ───────────────────────────────────────────────────────────────────

    def _company_ids_sql(self):
        ids = self.env.companies.ids or [0]
        return ','.join(map(str, ids))

    def _select(self):
        return """
            fl.id,
            fl.company_id,
            fl.product_id,
            pt.categ_id,
            pt.uom_id,
            sp.partner_id,
            out_sm.picking_id,
            COALESCE(sp.name, out_sm.reference)                     AS reference,
            out_sm.date::date                                       AS outgoing_date,
            fl.incoming_date::date                                  AS incoming_date,
            fl.quantity,
            fl.unit_cost,
            fl.outgoing_value,
            CASE
                WHEN dest_loc.scrap_location = TRUE                 THEN 'scrap'
                WHEN dest_loc.usage = 'supplier'                    THEN 'purchase_return'
                WHEN dest_loc.usage = 'customer'                    THEN 'sale'
                WHEN dest_loc.usage = 'inventory'                   THEN 'inventory'
                WHEN dest_loc.usage = 'internal'                    THEN 'internal'
                ELSE                                                     'other'
            END                                                     AS outgoing_usage
        """

    def _from(self):
        return """
            odooer_fifo_link fl
            INNER JOIN stock_move out_sm ON out_sm.id = fl.outgoing_move_id
            INNER JOIN product_product pp ON pp.id = fl.product_id
            INNER JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN stock_picking sp ON sp.id = out_sm.picking_id
            LEFT JOIN stock_location dest_loc ON dest_loc.id = out_sm.location_dest_id
        """

    def _where(self):
        return "fl.company_id IN ({})".format(self._company_ids_sql())

    @property
    def _table_query(self):
        return """
            SELECT {select}
            FROM   {from_}
            WHERE  {where}
        """.format(
            select=self._select(),
            from_=self._from(),
            where=self._where(),
        )
