# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import fields, models, api
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
    cogs_account_id = fields.Many2one('account.account', string='COGS Account', readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    categ_id = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', readonly=True)
    product_type = fields.Selection(
        selection=[('consu', 'Goods'), ('service', 'Service'), ('combo', 'Combo')],
        string='Product Type', readonly=True,
    )
    date = fields.Date(string='Order Date', readonly=True)
    name = fields.Char(string='Description', readonly=True)
    ordered_qty = fields.Float(
        string='Ordered Qty', digits='Product Unit of Measure', readonly=True,
    )

    # ── Measures ──────────────────────────────────────────────────────────────
    invoiced_qty = fields.Float(
        string='Invoiced Qty', digits='Product Unit of Measure', readonly=True,
    )
    invoiced_total = fields.Float(
        string='Revenue', digits='Product Price', readonly=True,
    )
    moved_qty = fields.Float(
        string='Delivered Qty (Product UoM)', digits='Product Unit of Measure', readonly=True,
    )
    moved_uom_id = fields.Many2one(
        'uom.uom', string='Product UoM', readonly=True,
    )
    moved_uom_qty = fields.Float(
        string='Delivered Qty', digits='Product Unit of Measure', readonly=True,
        help="Delivered quantity expressed in the ordered unit of measure (uom_id).",
    )
    qty_diff = fields.Float(
        string='Invoiced − Delivered', digits='Product Unit of Measure', readonly=True,
        help="Invoiced Qty minus Delivered Qty (both in ordered UoM). "
             "Positive = more invoiced than delivered; Negative = more delivered than invoiced.",
    )
    moved_qty_display = fields.Char(
        string='Delivered Qty', compute='_compute_moved_qty_display', readonly=True,
    )
    post_period_invoiced_qty = fields.Float(
        string='Post-Period Invoiced', digits='Product Unit of Measure', readonly=True,
        help="Invoiced quantity between the report end date and today.",
    )
    post_period_revenue = fields.Float(
        string='Post-Period Revenue', digits='Product Price', readonly=True,
        help="Revenue from invoices between the report end date and today.",
    )
    expected_revenue = fields.Float(
        string='Expected Revenue', digits='Product Price', readonly=True,
        help="Delivered-but-not-invoiced qty (as of report end) × unit sale price.",
    )
    post_period_delivered_qty = fields.Float(
        string='Post-Period Delivered', digits='Product Unit of Measure', readonly=True,
        help="Delivered quantity (in ordered UoM) between the report end date and today.",
    )
    post_period_cogs = fields.Float(
        string='Post-Period COGS', digits='Product Price', readonly=True,
        help="FIFO cost for deliveries between the report end date and today.",
    )
    expected_cogs = fields.Float(
        string='Expected COGS', digits='Product Price', readonly=True,
        help="Invoiced unit cost × qty still undelivered as of today (invoiced upto end − delivered upto end).",
    )
    invoiced_price = fields.Float(
        string='Invoiced Unit Price', digits='Product Price', readonly=True,
        help="Average unit sale price from invoice lines (revenue / invoiced qty).",
    )
    invoiced_cost = fields.Float(
        string='Invoiced Unit Cost', digits='Product Price', readonly=True,
        help="COGS per unit from invoice journal entries (expense_direct_cost / invoiced qty).",
    )
    standard_price = fields.Float(
        string='Standard Cost', digits='Product Price', readonly=True,
        help="Product standard cost from the product form, converted to order UoM.",
    )
    cogs = fields.Float(
        string='COGS', digits='Product Price', readonly=True,
        help="Cost of Goods Sold — FIFO cost attributed to deliveries in this period.",
    )
    gp = fields.Float(
        string='Gross Profit', digits='Product Price', readonly=True,
        help="Revenue minus COGS for the selected period.",
    )
    margin_pct = fields.Float(
        string='Margin %', digits=(5, 2), readonly=True,
        compute='_compute_margin_pct',
        help="Gross Profit / Revenue × 100",
    )
    currency_id = fields.Many2one(
        'res.currency', string='Currency', readonly=True,
        compute='_compute_currency_id',
    )

    # ── Related records for detail dialog ────────────────────────────────────
    invoice_ids = fields.Many2many(
        'account.move', string='Invoices',
        compute='_compute_detail_records',
    )
    delivery_move_ids = fields.Many2many(
        'stock.move', string='Deliveries',
        compute='_compute_detail_records',
    )
    fifo_link_ids = fields.Many2many(
        'odooer.fifo.link', string='FIFO Sources',
        compute='_compute_detail_records',
    )

    def _compute_currency_id(self):
        for rec in self:
            rec.currency_id = rec.company_id.currency_id

    @api.depends('gp', 'invoiced_total')
    def _compute_margin_pct(self):
        for rec in self:
            rec.margin_pct = (rec.gp / rec.invoiced_total * 100) if rec.invoiced_total else 0.0

    @api.depends('moved_uom_qty', 'uom_id', 'moved_qty', 'moved_uom_id')
    def _compute_moved_qty_display(self):
        def _fmt(qty):
            qty = qty or 0.0
            n = round(qty, 6)
            if n == int(n):
                return str(int(n))
            s = f"{n:.6f}".rstrip('0')
            return s

        for rec in self:
            order_uom = rec.uom_id.name or ''
            prod_uom = rec.moved_uom_id.name or ''
            uom_qty = _fmt(rec.moved_uom_qty)
            if not prod_uom or order_uom == prod_uom:
                rec.moved_qty_display = f"{uom_qty} {order_uom}".strip()
            else:
                prod_qty = _fmt(rec.moved_qty)
                rec.moved_qty_display = f"{uom_qty} {order_uom} = {prod_qty} {prod_uom}"

    def _compute_detail_records(self):
        """Load invoices, delivery moves and FIFO links for the detail dialog."""
        SaleOrderLine = self.env['sale.order.line']
        StockMove = self.env['stock.move']
        FifoLink = self.env['odooer.fifo.link']

        for rec in self:
            sol = SaleOrderLine.browse(rec.id)

            # Invoices via the standard sale → invoice relation
            inv_line_ids = self.env['account.move.line'].search([
                ('id', 'in', sol.invoice_lines.ids),
            ])
            rec.invoice_ids = inv_line_ids.mapped('move_id')

            # Delivery moves: outgoing to customer + returns from customer
            moves = StockMove.search([
                ('sale_line_id', '=', rec.id),
                ('state', '=', 'done'),
            ])
            # Also returns where sale_line_id was set on origin
            return_moves = StockMove.search([
                ('origin_returned_move_id', 'in', moves.ids),
                ('state', '=', 'done'),
            ]).filtered(lambda m: not m.sale_line_id)
            rec.delivery_move_ids = moves | return_moves

            # FIFO links for all outgoing moves of this sale line
            out_moves = moves.filtered(lambda m: m.is_out)
            rec.fifo_link_ids = FifoLink.search([
                ('outgoing_move_id', 'in', out_moves.ids),
            ])

    def action_gp_at_date(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'odooer.gp.report.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def action_open_detail(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'views': [(self.env.ref('odooer_stock.odooer_gp_report_form').id, 'form')],
            'target': 'new',
            'flags': {'mode': 'readonly'},
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
            -- All invoiced up to and including report end (includes pre-period)
            sale_upto_end AS (
                SELECT
                    ilr.order_line_id AS sale_line_id,
                    SUM(
                        CASE am.move_type WHEN 'out_refund' THEN -1 ELSE 1 END
                        * aml.quantity
                    ) AS invoiced_qty
                FROM account_move_line aml
                INNER JOIN sale_order_line_invoice_rel ilr
                        ON ilr.invoice_line_id = aml.id
                INNER JOIN account_move am ON am.id = aml.move_id
                WHERE am.state = 'posted'
                  AND aml.date <= '{end}'
                GROUP BY ilr.order_line_id
            ),
            -- Invoiced after report end (post-period)
            sale_post AS (
                SELECT
                    ilr.order_line_id AS sale_line_id,
                    SUM(
                        CASE am.move_type WHEN 'out_refund' THEN -1 ELSE 1 END
                        * aml.quantity
                    ) AS invoiced_qty,
                    SUM(aml.balance * -1) AS invoiced_total
                FROM account_move_line aml
                INNER JOIN sale_order_line_invoice_rel ilr
                        ON ilr.invoice_line_id = aml.id
                INNER JOIN account_move am ON am.id = aml.move_id
                WHERE am.state = 'posted'
                  AND aml.date > '{end}'
                GROUP BY ilr.order_line_id
            ),
            -- COGS amount from invoice lines, proportionally split by revenue qty
            cogs_invoice AS (
                SELECT
                    ilr.order_line_id AS sale_line_id,
                    SUM(
                        ipc.total_cogs
                        * aml_rev.quantity
                        / NULLIF(SUM(aml_rev.quantity) OVER (
                            PARTITION BY am.id, aml_rev.product_id
                        ), 0)
                    ) AS cogs_total
                FROM account_move_line aml_rev
                INNER JOIN sale_order_line_invoice_rel ilr
                        ON ilr.invoice_line_id = aml_rev.id
                INNER JOIN account_move am ON am.id = aml_rev.move_id
                INNER JOIN (
                    SELECT
                        aml_cogs.move_id,
                        aml_cogs.product_id,
                        SUM(aml_cogs.balance) AS total_cogs
                    FROM account_move_line aml_cogs
                    INNER JOIN account_account aa ON aa.id = aml_cogs.account_id
                    WHERE aa.account_type = 'expense_direct_cost'
                    GROUP BY aml_cogs.move_id, aml_cogs.product_id
                ) ipc ON ipc.move_id = am.id
                     AND ipc.product_id = aml_rev.product_id
                WHERE am.state = 'posted'
                  AND aml_rev.date BETWEEN '{start}' AND '{end}'
                GROUP BY ilr.order_line_id
            ),
            -- All relevant done moves (no date filter) — partitioned below
            cost_moves AS (
                SELECT
                    COALESCE(sm.sale_line_id, orig.sale_line_id)             AS sale_line_id,
                    sm.date::date                                             AS move_date,
                    CASE WHEN sm.is_out THEN 1 ELSE -1 END
                        * COALESCE(sml_qty.qty, 0)                           AS moved_qty,
                    (CASE WHEN sm.is_out
                          THEN COALESCE(fl_agg.fifo_value, 0)
                          ELSE sm.value
                     END
                     * CASE WHEN sm.is_out THEN 1 ELSE -1 END)               AS cogs_value
                FROM stock_move sm
                JOIN stock_location sl_src ON sl_src.id = sm.location_id
                LEFT JOIN stock_move orig ON orig.id = sm.origin_returned_move_id
                LEFT JOIN (
                    SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                    FROM stock_move_line sml
                    WHERE sml.state = 'done'
                    GROUP BY sml.move_id
                ) sml_qty ON sml_qty.move_id = sm.id
                LEFT JOIN (
                    SELECT outgoing_move_id, SUM(outgoing_value) AS fifo_value
                    FROM odooer_fifo_link
                    GROUP BY outgoing_move_id
                ) fl_agg ON fl_agg.outgoing_move_id = sm.id
                WHERE sm.state = 'done'
                  AND (
                      (sm.is_out = TRUE AND sm.sale_line_id IS NOT NULL)
                      OR
                      (sm.is_in = TRUE AND sl_src.usage = 'customer'
                       AND (sm.sale_line_id IS NOT NULL OR orig.sale_line_id IS NOT NULL))
                  )
            ),
            -- In-period deliveries
            cost AS (
                SELECT sale_line_id,
                       SUM(moved_qty)  AS moved_qty,
                       SUM(cogs_value) AS cogs
                FROM cost_moves
                WHERE move_date BETWEEN '{start}' AND '{end}'
                GROUP BY sale_line_id
            ),
            -- Post-period deliveries (end+1 day → today)
            cost_post AS (
                SELECT sale_line_id,
                       SUM(moved_qty)  AS moved_qty,
                       SUM(cogs_value) AS cogs
                FROM cost_moves
                WHERE move_date > '{end}'
                GROUP BY sale_line_id
            ),
            -- All delivered up to and including report end (includes pre-period)
            cost_upto_end AS (
                SELECT sale_line_id,
                       SUM(moved_qty) AS moved_qty
                FROM cost_moves
                WHERE move_date <= '{end}'
                GROUP BY sale_line_id
            ),
            -- Company-wide fallback COGS account (ir.default)
            cogs_acct_default AS (
                SELECT d.company_id, (d.json_value)::int AS account_id
                FROM ir_default d
                JOIN ir_model_fields f ON f.id = d.field_id
                WHERE f.name  = 'property_account_expense_categ_id'
                  AND f.model = 'product.category'
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
            -- COGS account: direct category → parent → grandparent → ir.default
            COALESCE(
                (pc.property_account_expense_categ_id
                    ->>(COALESCE(sale.company_id, so.company_id)::text))::int,
                (pc2.property_account_expense_categ_id
                    ->>(COALESCE(sale.company_id, so.company_id)::text))::int,
                (pc3.property_account_expense_categ_id
                    ->>(COALESCE(sale.company_id, so.company_id)::text))::int,
                cad.account_id
            )                                                                AS cogs_account_id,
            sol.product_id,
            pt.categ_id,
            pt.type                                                          AS product_type,
            sol.product_uom_id                                               AS uom_id,
            sol.product_uom_qty                                              AS ordered_qty,
            SUM(sale.invoiced_qty)                                           AS invoiced_qty,
            SUM(sale.invoiced_total)                                         AS invoiced_total,
            SUM(cost.moved_qty)                                              AS moved_qty,
            pt.uom_id                                                        AS moved_uom_id,
            SUM(COALESCE(cost.moved_qty, 0))
                * COALESCE(prod_uom.factor / NULLIF(order_uom.factor, 0), 1) AS moved_uom_qty,
            ROUND((
                COALESCE(SUM(sale.invoiced_qty), 0)
                - SUM(COALESCE(cost.moved_qty, 0))
                * COALESCE(prod_uom.factor / NULLIF(order_uom.factor, 0), 1)
            )::numeric, 6)                                                   AS qty_diff,
            SUM(cost.cogs)                                                   AS cogs,
            SUM(COALESCE(sale.invoiced_total, 0) - COALESCE(cost.cogs, 0))   AS gp,
            -- Post-period invoiced qty (ordered UoM)
            SUM(COALESCE(sale_post.invoiced_qty, 0))                         AS post_period_invoiced_qty,
            -- Post-period revenue
            SUM(COALESCE(sale_post.invoiced_total, 0))                       AS post_period_revenue,
            -- Expected Revenue: delivered-but-not-invoiced qty (up to end) × unit price
            -- Uses total invoiced as of today (incl. post-period) to avoid false positives
            GREATEST(0.0,
                COALESCE(SUM(cost_upto_end.moved_qty), 0)
                    * COALESCE(prod_uom.factor / NULLIF(order_uom.factor, 0), 1)
                - (COALESCE(SUM(sale_upto_end.invoiced_qty), 0)
                   + COALESCE(SUM(sale_post.invoiced_qty), 0))
            ) * COALESCE(sol.price_reduce_taxexcl, 0.0)                      AS expected_revenue,
            -- Post-period delivered qty (in ordered UoM)
            SUM(COALESCE(cost_post.moved_qty, 0))
                * COALESCE(prod_uom.factor / NULLIF(order_uom.factor, 0), 1) AS post_period_delivered_qty,
            -- Post-period COGS
            SUM(COALESCE(cost_post.cogs, 0))                                 AS post_period_cogs,
            -- Expected COGS: invoiced-but-not-delivered qty (up to end) × weighted avg FIFO unit cost
            GREATEST(0.0,
                COALESCE(SUM(sale_upto_end.invoiced_qty), 0)
                    * COALESCE(order_uom.factor / NULLIF(prod_uom.factor, 0), 1)
                - COALESCE(SUM(cost_upto_end.moved_qty), 0)
            ) * COALESCE(
                SUM(cogs_invoice.cogs_total) / NULLIF(SUM(sale.invoiced_qty), 0),
                0.0
            )                                                                    AS expected_cogs,
            -- Invoiced unit price (average sale price across all invoice lines)
            CASE WHEN SUM(sale.invoiced_qty) != 0
                 THEN SUM(sale.invoiced_total) / SUM(sale.invoiced_qty)
                 ELSE 0.0
            END                                                                   AS invoiced_price,
            -- Invoiced unit cost = COGS from invoice journal entries / invoiced qty
            COALESCE(
                SUM(cogs_invoice.cogs_total) / NULLIF(SUM(sale.invoiced_qty), 0),
                0.0
            )                                                                      AS invoiced_cost,
            -- Product standard cost (JSONB, company-dependent), converted from product UoM to order UoM
            COALESCE(
                (pp.standard_price->>(COALESCE(sale.company_id, so.company_id)::text))::float,
                0.0
            ) * COALESCE(order_uom.factor / NULLIF(prod_uom.factor, 0), 1)       AS standard_price
        """

    def _from(self):
        return """
            sale
            RIGHT JOIN sale_order_line sol ON sol.id = sale.sale_line_id
            INNER JOIN sale_order so ON so.id = sol.order_id
            LEFT JOIN product_product pp ON pp.id = sol.product_id
            LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN product_category  pc  ON pc.id  = pt.categ_id
            LEFT JOIN product_category  pc2 ON pc2.id = pc.parent_id
            LEFT JOIN product_category  pc3 ON pc3.id = pc2.parent_id
            LEFT JOIN cogs_acct_default cad
                   ON cad.company_id = COALESCE(sale.company_id, so.company_id)
            LEFT JOIN cost ON sol.id = cost.sale_line_id
            LEFT JOIN cost_post    ON sol.id = cost_post.sale_line_id
            LEFT JOIN cost_upto_end ON sol.id = cost_upto_end.sale_line_id
            LEFT JOIN cogs_invoice ON sol.id = cogs_invoice.sale_line_id
            LEFT JOIN sale_upto_end ON sol.id = sale_upto_end.sale_line_id
            LEFT JOIN sale_post    ON sol.id = sale_post.sale_line_id
            LEFT JOIN uom_uom prod_uom  ON prod_uom.id  = pt.uom_id
            LEFT JOIN uom_uom order_uom ON order_uom.id = sol.product_uom_id
        """

    def _where(self):
        return (
            "(sale.sale_line_id IS NOT NULL OR cost.sale_line_id IS NOT NULL)"
            " AND so.company_id IN ({})".format(self._company_ids_sql())
        )

    def _group_by(self):
        return (
            "sol.id, so.id, sale.account_id, sale.company_id, so.company_id, "
            "pt.categ_id, pt.type, pt.uom_id, sol.product_uom_id, sol.product_uom_qty, "
            "sol.price_reduce_taxexcl, "
            "prod_uom.factor, order_uom.factor, "
            "pp.standard_price, "
            "pc.property_account_expense_categ_id, "
            "pc2.property_account_expense_categ_id, "
            "pc3.property_account_expense_categ_id, "
            "cad.account_id"
        )

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

