# -*- coding: utf-8 -*-
from datetime import date as date_type
from odoo import api, fields, models
from odoo.tools.misc import format_date


class OdooerValuationReport(models.Model):
    """
    Inventory valuation snapshot report.

    _auto = False model — backed by a dynamic SQL subquery (_table_query).
    The 'as_of' date is read from context key 'search_as_of'; defaults to today.

    One row per incoming stock move.  remaining_qty / remaining_value are
    computed as of the context date so the user can pivot, filter, and
    group-by freely.
    """
    _name = 'odooer.valuation.report'
    _description = 'Odooer Inventory Valuation Report'
    _auto = False
    _order = 'incoming_date desc, id'
    _rec_name = 'reference'

    def _compute_display_name(self):
        for rec in self:
            product = rec.product_id.display_name or ''
            date = str(rec.incoming_date) if rec.incoming_date else ''
            ref = rec.reference or ''
            rec.display_name = f"{ref} – {product}" if ref else f"{product} ({date})"

    def get_formview_action(self, access_uid=None):
        """Open the detail form as a dialog instead of navigating to a new route."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'views': [[False, 'form']],
            'target': 'new',
        }

    # ── Dimensions ────────────────────────────────────────────────────────────
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    picking_id = fields.Many2one('stock.picking', string='Receipt', readonly=True)
    production_id = fields.Many2one('mrp.production', string='Mfg Order', readonly=True)
    reference = fields.Char(string='Reference', readonly=True)
    location_dest_id = fields.Many2one('stock.location', string='Location', readonly=True)
    incoming_date = fields.Date(string='Incoming Date', readonly=True)
    incoming_type = fields.Selection(
        selection=[
            ('purchase', 'Purchase'),
            ('manufacturing', 'Manufacturing'),
            ('sale_return', 'Sale Return'),
            ('inventory', 'Inventory Adjustment'),
            ('other', 'Other'),
        ],
        string='Incoming Type',
        readonly=True,
    )

    # ── Measures ──────────────────────────────────────────────────────────────
    quantity = fields.Float(string='Incoming Qty', digits='Product Unit of Measure', readonly=True)
    unit_cost = fields.Float(string='Unit Cost', digits='Product Price', readonly=True)
    total_value = fields.Float(string='Incoming Value', digits='Product Price', readonly=True)
    remaining_qty = fields.Float(string='Remaining Qty', digits='Product Unit of Measure', readonly=True)
    remaining_value = fields.Float(string='Remaining Value', digits='Product Price', readonly=True)

    # ── Detail relations (used in form popup) ─────────────────────────────────
    # id = sm.id, so move_id and id are the same value — exposed as Many2one for clickability
    move_id = fields.Many2one('stock.move', string='Stock Move', readonly=True)
    value_source_ids = fields.One2many(
        'odooer.valuation.source', 'incoming_move_id',
        string='Value Sources', readonly=True)
    outgoing_ids = fields.One2many(
        'odooer.outgoing.detail', 'incoming_move_id',
        string='Outgoing Moves', readonly=True)

    def action_open_at_date(self):
        """Open the date-picker wizard."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'odooer.valuation.report.wizard',
            'views': [[False, 'form']],
            'target': 'new',
        }

    # ── SQL ───────────────────────────────────────────────────────────────────

    def _as_of(self):
        return self.env.context.get('search_as_of') or date_type.today()

    def _company_ids_sql(self):
        ids = self.env.companies.ids or [0]
        return ','.join(map(str, ids))

    def _with(self):
        as_of = self._as_of()
        return """
            consumed_by_incoming AS (
                SELECT
                    fl.incoming_move_id,
                    SUM(fl.quantity) AS consumed_qty
                FROM odooer_fifo_link fl
                INNER JOIN stock_move out_sm ON out_sm.id = fl.outgoing_move_id
                WHERE out_sm.date::date <= '{as_of}'
                GROUP BY fl.incoming_move_id
            )
        """.format(as_of=as_of)

    def _has_column(self, table, column):
        """Check if a column exists (used to guard optional module columns)."""
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=%s AND column_name=%s",
            (table, column),
        )
        return bool(self.env.cr.fetchone())

    def _select(self):
        return """
            sm.id,
            sm.id                                                              AS move_id,
            sm.company_id,
            sm.product_id,
            pt.categ_id,
            pt.uom_id,
            sp.partner_id,
            sm.picking_id,
            {production_id_sql}
            COALESCE(sp.name, {mo_name_sql})                                       AS reference,
            sm.location_dest_id,
            sm.date::date                                                          AS incoming_date,
            -- qty_prod_uom: actual done quantity from move lines (already in product's default UOM)
            COALESCE(sml_qty.qty, 0)                                               AS quantity,
            CASE WHEN COALESCE(sml_qty.qty, 0) > 0
                 THEN sm.value / sml_qty.qty
                 ELSE 0 END                                                        AS unit_cost,
            sm.value                                                               AS total_value,
            COALESCE(sml_qty.qty, 0)
                - COALESCE(cbi.consumed_qty, 0)                                    AS remaining_qty,
            (COALESCE(sml_qty.qty, 0) - COALESCE(cbi.consumed_qty, 0))
                * CASE WHEN COALESCE(sml_qty.qty, 0) > 0
                       THEN sm.value / sml_qty.qty
                       ELSE 0 END                                                  AS remaining_value,
            CASE
                WHEN sm.purchase_line_id IS NOT NULL            THEN 'purchase'
                {manufacturing_case}
                WHEN src_loc.usage = 'customer'                 THEN 'sale_return'
                WHEN src_loc.usage = 'inventory'                THEN 'inventory'
                ELSE 'other'
            END                                                                    AS incoming_type
        """.format(
            manufacturing_case=self._manufacturing_case(),
            production_id_sql=self._production_id_sql(),
            mo_name_sql=self._mo_name_sql(),
        )

    def _manufacturing_case(self):
        if self._has_column('stock_move', 'production_id'):
            return """
                WHEN sm.production_id IS NOT NULL      THEN 'manufacturing'
                WHEN sm.unbuild_id IS NOT NULL         THEN 'manufacturing'
                WHEN sm.consume_unbuild_id IS NOT NULL THEN 'manufacturing'"""
        return ""

    def _production_id_sql(self):
        if self._has_column('stock_move', 'production_id'):
            return "sm.production_id,"
        return "NULL::integer AS production_id,"

    def _mo_name_sql(self):
        """SQL expression for MO/unbuild name; NULL literal when MRP not installed."""
        if self._has_column('stock_move', 'production_id'):
            return "COALESCE(mo.name, ub.name)"
        return "NULL::varchar"

    def _from(self):
        mrp_installed = self._has_column('stock_move', 'production_id')
        mo_join = (
            "LEFT JOIN mrp_production mo ON mo.id = sm.production_id\n"
            "            LEFT JOIN mrp_unbuild ub ON ub.id = COALESCE(sm.unbuild_id, sm.consume_unbuild_id)"
        ) if mrp_installed else ""
        return """
            stock_move sm
            LEFT JOIN consumed_by_incoming cbi ON cbi.incoming_move_id = sm.id
            INNER JOIN product_product pp ON pp.id = sm.product_id
            INNER JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN stock_picking sp ON sp.id = sm.picking_id
            {mo_join}
            LEFT JOIN stock_location src_loc ON src_loc.id = sm.location_id
            LEFT JOIN (
                SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                FROM stock_move_line sml
                WHERE sml.state = 'done'
                GROUP BY sml.move_id
            ) sml_qty ON sml_qty.move_id = sm.id
        """.format(mo_join=mo_join)

    def _where(self):
        as_of = self._as_of()
        company_ids = self._company_ids_sql()
        return """
            sm.is_in      = TRUE
            AND sm.state  = 'done'
            AND sm.date::date <= '{as_of}'
            AND sm.company_id IN ({company_ids})
            AND COALESCE(sml_qty.qty, 0) > 0
        """.format(as_of=as_of, company_ids=company_ids)

    @property
    def _table_query(self):
        return """
            WITH {with_}
            SELECT {select}
            FROM   {from_}
            WHERE  {where}
        """.format(
            with_=self._with(),
            select=self._select(),
            from_=self._from(),
            where=self._where(),
        )


class OdooerValuationReportWizard(models.TransientModel):
    """Small date-picker that re-opens the report with search_as_of context."""
    _name = 'odooer.valuation.report.wizard'
    _description = 'Odooer Valuation Report – Date Picker'

    as_of = fields.Date(
        string='As Of Date',
        required=True,
        default=lambda self: fields.Date.context_today(self),
    )

    def open_at_date(self):
        action = self.env['ir.actions.actions']._for_xml_id(
            'odooer_stock.action_odooer_valuation_report'
        )
        action['display_name'] = 'Inventory Valuation – %s' % format_date(self.env, self.as_of)
        action['context'] = {'search_as_of': str(self.as_of)}
        return action
