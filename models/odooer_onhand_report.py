# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.tools.misc import format_date


class OdooerOnhandReport(models.Model):
    """
    Product-level inventory on-hand summary.

    One row per product (per company). Combines:
      - FIFO unit cost  = total_fifo_remaining_value / total_fifo_remaining_qty
      - On-hand qty/value = stock_quant for *internal* locations only (excl. transit)
      - Transit qty/value = stock_quant for *transit* locations
      - Free qty  = on_hand_qty - reserved_qty (internal)
    """
    _name = 'odooer.onhand.report'
    _description = 'Odooer Stock On-Hand Report'
    _auto = False
    _order = 'product_id'

    # ── Dimensions ────────────────────────────────────────────────────────────
    company_id  = fields.Many2one('res.company', string='Company', readonly=True)
    product_id  = fields.Many2one('product.product', string='Product', readonly=True)
    categ_id    = fields.Many2one('product.category', string='Category', readonly=True)
    uom_id      = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    stock_account_id = fields.Many2one('account.account', string='Stock Account', readonly=True)

    # ── Measures ──────────────────────────────────────────────────────────────
    unit_cost          = fields.Float(string='FIFO Unit Cost',     digits='Product Price',            readonly=True)
    onhand_qty         = fields.Float(string='On Hand',            digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    onhand_value       = fields.Float(string='On Hand Value',      digits='Product Price',            readonly=True, aggregator='sum')
    free_qty           = fields.Float(string='Free to Use',        digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    transit_qty        = fields.Float(string='In Transit',         digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    transit_value      = fields.Float(string='Transit Value',      digits='Product Price',            readonly=True, aggregator='sum')
    fifo_remaining_qty = fields.Float(string='Remaining Qty',      digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    fifo_remaining_value = fields.Float(string='Remaining Value',  digits='Product Price',            readonly=True, aggregator='sum')

    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        compute='_compute_currency_id',
    )

    @api.depends('company_id')
    def _compute_currency_id(self):
        for rec in self:
            rec.currency_id = (rec.company_id or self.env.company).currency_id

    def action_open_at_date(self):
        """Open the date-picker wizard."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'odooer.onhand.report.wizard',
            'views': [[False, 'form']],
            'target': 'new',
        }

    def action_open_detail(self):
        """Drill-down to the per-lot FIFO valuation report for this product."""
        return {
            'type': 'ir.actions.act_window',
            'name': 'FIFO Valuation Detail',
            'res_model': 'odooer.valuation.report',
            'views': [[False, 'list'], [False, 'pivot']],
            'domain': [('product_id', '=', self.product_id.id)],
            'target': 'current',
        }

    # ── SQL ───────────────────────────────────────────────────────────────────

    def _as_of(self):
        return self.env.context.get('search_as_of') or fields.Date.context_today(self)

    def _company_ids_sql(self):
        ids = self.env.companies.ids or [0]
        return ','.join(map(str, ids))

    def _with(self):
        company_ids = self._company_ids_sql()
        as_of = self._as_of()
        return """
            -- Positive FIFO remaining from incoming moves as of {as_of}
            raw_fifo AS (
                SELECT
                    sm.product_id,
                    sm.company_id,
                    COALESCE(sml_qty.qty, 0) - COALESCE(consumed.qty, 0)       AS remaining_qty,
                    (COALESCE(sml_qty.qty, 0) - COALESCE(consumed.qty, 0))
                    * CASE WHEN COALESCE(sml_qty.qty, 0) > 0
                           THEN sm.value / sml_qty.qty
                           ELSE 0 END                                           AS remaining_value
                FROM stock_move sm
                LEFT JOIN (
                    SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                    FROM stock_move_line sml
                    WHERE sml.state = 'done'
                    GROUP BY sml.move_id
                ) sml_qty ON sml_qty.move_id = sm.id
                LEFT JOIN (
                    SELECT fl.incoming_move_id, SUM(fl.quantity) AS qty
                    FROM odooer_fifo_link fl
                    JOIN stock_move out_sm ON out_sm.id = fl.outgoing_move_id
                    WHERE fl.incoming_move_id IS NOT NULL
                      AND out_sm.date::date <= '{as_of}'
                    GROUP BY fl.incoming_move_id
                ) consumed ON consumed.incoming_move_id = sm.id
                WHERE sm.is_in          = TRUE
                  AND sm.state          = 'done'
                  AND sm.date::date    <= '{as_of}'
                  AND COALESCE(sml_qty.qty, 0) > 0
                  AND sm.company_id IN ({company_ids})
            ),

            -- Overflow / negative-stock debt: consumption beyond the FIFO stack
            -- (links with incoming_move_id IS NULL). Subtracted from the total.
            overflow AS (
                SELECT
                    fl.product_id,
                    fl.company_id,
                    -SUM(fl.quantity)        AS remaining_qty,
                    -SUM(fl.outgoing_value)  AS remaining_value
                FROM odooer_fifo_link fl
                JOIN stock_move out_sm ON out_sm.id = fl.outgoing_move_id
                WHERE fl.incoming_move_id IS NULL
                  AND out_sm.date::date <= '{as_of}'
                  AND fl.company_id IN ({company_ids})
                GROUP BY fl.product_id, fl.company_id
            ),

            -- Combined FIFO summary: raw remaining minus overflow debt
            fifo_summary AS (
                SELECT
                    product_id,
                    company_id,
                    SUM(remaining_qty)   AS fifo_remaining_qty,
                    SUM(remaining_value) AS fifo_remaining_value
                FROM (
                    SELECT product_id, company_id, remaining_qty, remaining_value FROM raw_fifo
                    UNION ALL
                    SELECT product_id, company_id, remaining_qty, remaining_value FROM overflow
                ) combined
                GROUP BY product_id, company_id
            ),

            -- On-hand in internal locations as of {as_of} (reconstructed from move lines)
            onhand AS (
                SELECT sml.product_id,
                       sm.company_id,
                       SUM(
                           CASE WHEN sl_dest.usage = 'internal' THEN sml.quantity_product_uom ELSE 0 END
                         - CASE WHEN sl_src.usage  = 'internal' THEN sml.quantity_product_uom ELSE 0 END
                       )  AS qty,
                       0::numeric AS reserved_qty
                FROM stock_move_line sml
                JOIN stock_move sm       ON sm.id  = sml.move_id
                JOIN stock_location sl_src  ON sl_src.id  = sml.location_id
                JOIN stock_location sl_dest ON sl_dest.id = sml.location_dest_id
                WHERE sml.state = 'done'
                  AND sml.date::date <= '{as_of}'
                  AND sm.company_id IN ({company_ids})
                  AND (sl_src.usage = 'internal' OR sl_dest.usage = 'internal')
                GROUP BY sml.product_id, sm.company_id
            ),

            -- Qty in transit locations as of {as_of}
            transit AS (
                SELECT sml.product_id,
                       sm.company_id,
                       SUM(
                           CASE WHEN sl_dest.usage = 'transit' THEN sml.quantity_product_uom ELSE 0 END
                         - CASE WHEN sl_src.usage  = 'transit' THEN sml.quantity_product_uom ELSE 0 END
                       )  AS qty
                FROM stock_move_line sml
                JOIN stock_move sm       ON sm.id  = sml.move_id
                JOIN stock_location sl_src  ON sl_src.id  = sml.location_id
                JOIN stock_location sl_dest ON sl_dest.id = sml.location_dest_id
                WHERE sml.state = 'done'
                  AND sml.date::date <= '{as_of}'
                  AND sm.company_id IN ({company_ids})
                  AND (sl_src.usage = 'transit' OR sl_dest.usage = 'transit')
                GROUP BY sml.product_id, sm.company_id
            ),

            -- Company-wide fallback stock valuation account (ir.default)
            stock_acct_default AS (
                SELECT d.company_id, (d.json_value)::int AS account_id
                FROM ir_default d
                JOIN ir_model_fields f ON f.id = d.field_id
                WHERE f.name  = 'property_stock_valuation_account_id'
                  AND f.model = 'product.category'
            )
        """.format(company_ids=company_ids, as_of=as_of)

    def _select(self):
        return """
            -- Stable row id: hash of (product_id, company_id)
            (fs.product_id * 100000 + fs.company_id)                           AS id,
            fs.company_id,
            fs.product_id,
            pt.categ_id,
            pt.uom_id,

            -- Stock valuation account: direct category → parent → ir.default fallback
            COALESCE(
                (pc.property_stock_valuation_account_id->>(fs.company_id::text))::int,
                (pc2.property_stock_valuation_account_id->>(fs.company_id::text))::int,
                (pc3.property_stock_valuation_account_id->>(fs.company_id::text))::int,
                sad.account_id
            )                                                                   AS stock_account_id,

            -- FIFO unit cost = remaining value / remaining qty
            -- Works for both positive and negative stock (negative / negative = positive)
            CASE WHEN fs.fifo_remaining_qty <> 0
                 THEN fs.fifo_remaining_value / fs.fifo_remaining_qty
                 ELSE 0 END                                                     AS unit_cost,

            fs.fifo_remaining_qty,
            fs.fifo_remaining_value,

            -- On-hand (internal only)
            COALESCE(oh.qty, 0)                                                 AS onhand_qty,
            CASE WHEN fs.fifo_remaining_qty <> 0
                 THEN fs.fifo_remaining_value / fs.fifo_remaining_qty
                 ELSE 0 END
                * COALESCE(oh.qty, 0)                                           AS onhand_value,

            -- Free to use
            COALESCE(oh.qty, 0) - COALESCE(oh.reserved_qty, 0)                 AS free_qty,

            -- In transit
            COALESCE(tr.qty, 0)                                                 AS transit_qty,
            CASE WHEN fs.fifo_remaining_qty <> 0
                 THEN fs.fifo_remaining_value / fs.fifo_remaining_qty
                 ELSE 0 END
                * COALESCE(tr.qty, 0)                                           AS transit_value
        """

    def _from(self):
        return """
            fifo_summary fs
            JOIN product_product pp ON pp.id = fs.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN product_category  pc  ON pc.id  = pt.categ_id
            LEFT JOIN product_category  pc2 ON pc2.id = pc.parent_id
            LEFT JOIN product_category  pc3 ON pc3.id = pc2.parent_id
            LEFT JOIN stock_acct_default sad ON sad.company_id = fs.company_id
            LEFT JOIN onhand  oh ON oh.product_id  = fs.product_id
                                 AND oh.company_id  = fs.company_id
            LEFT JOIN transit tr ON tr.product_id  = fs.product_id
                                 AND tr.company_id  = fs.company_id
        """

    @property
    def _table_query(self):
        return """
            WITH {with_}
            SELECT {select}
            FROM   {from_}
        """.format(
            with_=self._with(),
            select=self._select(),
            from_=self._from(),
        )


class OdooerOnhandReportWizard(models.TransientModel):
    """Date picker that re-opens the on-hand report with a search_as_of context."""
    _name = 'odooer.onhand.report.wizard'
    _description = 'Odooer On-Hand Report – Date Picker'

    as_of = fields.Date(
        string='As Of Date',
        required=True,
        default=lambda self: fields.Date.context_today(self),
    )

    def open_at_date(self):
        action = self.env['ir.actions.actions']._for_xml_id(
            'odooer_stock.action_odooer_onhand_report'
        )
        action['display_name'] = 'Stock On-Hand – %s' % format_date(self.env, self.as_of)
        action['context'] = {'search_as_of': str(self.as_of)}
        return action
