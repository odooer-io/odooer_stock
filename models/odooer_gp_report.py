# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import fields, models
from odoo.tools.misc import format_date


class OdooerGpReport(models.Model):
    """
    Sale gross profit report.

    One row per sale.order.line that has either invoiced revenue or delivered
    cost within the selected date period.

    Revenue  = sum of posted invoice lines linked to the sale line whose date
               falls in the chosen period.
    COGS     = sum of odooer_fifo_link.outgoing_value for outgoing moves whose
               done-date falls in the chosen period (via sale_line_id).
    GP       = Revenue - COGS

    Date range is passed via context keys:
      search_start_date  (str YYYY-MM-DD or date; defaults to 1st of current month)
      search_end_date    (str YYYY-MM-DD or date; defaults to last day of current month)
    """
    _name = 'odooer.gp.report'
    _description = 'Odooer Sale Gross Profit'
    _auto = False
    _order = 'date desc, id'

    # ── Dimensions ────────────────────────────────────────────────────────────
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    order_id = fields.Many2one('sale.order', string='Sale Order', readonly=True)
    order_line_id = fields.Many2one('sale.order.line', string='Sale Line', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    account_id = fields.Many2one('account.account', string='Revenue Account', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', readonly=True)
    date = fields.Date(string='Order Date', readonly=True)
    name = fields.Char(string='Description', readonly=True)

    # ── Measures ──────────────────────────────────────────────────────────────
    invoiced_qty = fields.Float(
        string='Invoiced Qty', digits='Product Unit of Measure', readonly=True,
    )
    invoiced_total = fields.Float(
        string='Revenue', digits='Product Price', readonly=True,
    )
    moved_qty = fields.Float(
        string='Delivered Qty', digits='Product Unit of Measure', readonly=True,
    )
    cogs = fields.Float(
        string='COGS', digits='Product Price', readonly=True,
        help="Cost of Goods Sold — FIFO cost attributed to deliveries in this period.",
    )
    gp = fields.Float(
        string='Gross Profit', digits='Product Price', readonly=True,
        help="Revenue minus COGS for the selected period.",
    )
    currency_id = fields.Many2one(
        'res.currency', string='Currency', readonly=True,
        compute='_compute_currency_id',
    )

    def _compute_currency_id(self):
        for rec in self:
            rec.currency_id = rec.company_id.currency_id

    def action_gp_at_date(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'odooer.gp.report.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _default_start(self):
        return datetime.today().replace(day=1).date()

    def _default_end(self):
        return (datetime.today().replace(day=1) + relativedelta(months=1) - timedelta(days=1)).date()

    def _date_range(self):
        start = self.env.context.get('search_start_date', self._default_start())
        end = self.env.context.get('search_end_date', self._default_end())
        return start, end

    def _company_ids_sql(self):
        ids = self.env.companies.ids or [0]
        return ','.join(map(str, ids))

    # ── SQL ───────────────────────────────────────────────────────────────────

    def _with(self):
        start, end = self._date_range()
        return """
            sale AS (
                SELECT
                    ilr.order_line_id                                        AS sale_line_id,
                    aml.company_id,
                    aml.account_id,
                    SUM(
                        CASE am.move_type WHEN 'out_refund' THEN -1 ELSE 1 END
                        * aml.quantity
                    )                                                        AS invoiced_qty,
                    SUM(aml.balance * -1)                                    AS invoiced_total
                FROM account_move_line aml
                INNER JOIN sale_order_line_invoice_rel ilr
                        ON ilr.invoice_line_id = aml.id
                INNER JOIN account_move am ON am.id = aml.move_id
                WHERE am.state = 'posted'
                  AND aml.date BETWEEN '{start}' AND '{end}'
                GROUP BY ilr.order_line_id, aml.company_id, aml.account_id
            ),
            cost AS (
                SELECT
                    sm.sale_line_id,
                    SUM(sm.quantity)                                         AS moved_qty,
                    COALESCE(SUM(fl_agg.fifo_value), 0)                      AS cogs
                FROM stock_move sm
                LEFT JOIN (
                    SELECT outgoing_move_id, SUM(outgoing_value) AS fifo_value
                    FROM odooer_fifo_link
                    GROUP BY outgoing_move_id
                ) fl_agg ON fl_agg.outgoing_move_id = sm.id
                WHERE sm.state = 'done'
                  AND sm.is_out = true
                  AND sm.sale_line_id IS NOT NULL
                  AND sm.date::date BETWEEN '{start}' AND '{end}'
                GROUP BY sm.sale_line_id
            )
        """.format(start=start, end=end)

    def _select(self):
        return """
            sol.id                                                           AS id,
            sol.id                                                           AS order_line_id,
            so.name || ' ' || sol.name                                       AS name,
            so.date_order::date                                              AS date,
            COALESCE(sale.company_id, so.company_id)                         AS company_id,
            so.id                                                            AS order_id,
            so.partner_id,
            sale.account_id,
            sol.product_id,
            pt.categ_id,
            sol.product_uom_id                                               AS uom_id,
            SUM(sale.invoiced_qty)                                           AS invoiced_qty,
            SUM(sale.invoiced_total)                                         AS invoiced_total,
            SUM(cost.moved_qty)                                              AS moved_qty,
            SUM(cost.cogs)                                                   AS cogs,
            SUM(COALESCE(sale.invoiced_total, 0) - COALESCE(cost.cogs, 0))   AS gp
        """

    def _from(self):
        return """
            sale
            RIGHT JOIN sale_order_line sol ON sol.id = sale.sale_line_id
            INNER JOIN sale_order so ON so.id = sol.order_id
            LEFT JOIN product_product pp ON pp.id = sol.product_id
            LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN cost ON sol.id = cost.sale_line_id
        """

    def _where(self):
        return (
            "(sale.sale_line_id IS NOT NULL OR cost.sale_line_id IS NOT NULL)"
            " AND so.company_id IN ({})".format(self._company_ids_sql())
        )

    def _group_by(self):
        return "sol.id, so.id, sale.account_id, sale.company_id"

    @property
    def _table_query(self):
        return """
            WITH {with_}
            SELECT {select}
            FROM   {from_}
            WHERE  {where}
            GROUP BY {group_by}
        """.format(
            with_=self._with(),
            select=self._select(),
            from_=self._from(),
            where=self._where(),
            group_by=self._group_by(),
        )


class OdooerGpReportWizard(models.TransientModel):
    """Date range picker for the GP report."""
    _name = 'odooer.gp.report.wizard'
    _description = 'Gross Profit Report Date Range'

    start_date = fields.Date(
        string='Start Date', required=True,
        default=lambda self: datetime.today().replace(day=1).date(),
    )
    end_date = fields.Date(
        string='End Date', required=True,
        default=lambda self: (
            datetime.today().replace(day=1) + relativedelta(months=1) - timedelta(days=1)
        ).date(),
    )

    def open_at_date(self):
        action = self.env['ir.actions.actions']._for_xml_id('odooer_stock.action_odooer_gp_report')
        action['display_name'] = '{} – {}'.format(
            format_date(self.env, self.start_date),
            format_date(self.env, self.end_date),
        )
        action['context'] = {
            'search_start_date': str(self.start_date),
            'search_end_date': str(self.end_date),
        }
        return action
