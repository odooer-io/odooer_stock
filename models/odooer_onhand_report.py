# -*- coding: utf-8 -*-
from odoo import api, fields, models


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

    # ── Measures ──────────────────────────────────────────────────────────────
    unit_cost          = fields.Float(string='FIFO Unit Cost',     digits='Product Price',            readonly=True)
    onhand_qty         = fields.Float(string='On Hand',            digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    onhand_value       = fields.Float(string='On Hand Value',      digits='Product Price',            readonly=True, aggregator='sum')
    free_qty           = fields.Float(string='Free to Use',        digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    transit_qty        = fields.Float(string='In Transit',         digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    transit_value      = fields.Float(string='Transit Value',      digits='Product Price',            readonly=True, aggregator='sum')
    fifo_remaining_qty = fields.Float(string='FIFO Remaining Qty', digits='Product Unit of Measure',  readonly=True, aggregator='sum')
    fifo_remaining_value = fields.Float(string='FIFO Remaining Value', digits='Product Price',        readonly=True, aggregator='sum')

    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        compute='_compute_currency_id',
    )

    @api.depends('company_id')
    def _compute_currency_id(self):
        for rec in self:
            rec.currency_id = (rec.company_id or self.env.company).currency_id

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

    def _company_ids_sql(self):
        ids = self.env.companies.ids or [0]
        return ','.join(map(str, ids))

    def _with(self):
        company_ids = self._company_ids_sql()
        return """
            -- FIFO remaining per product: sum across all incoming lots
            fifo_summary AS (
                SELECT
                    sm.product_id,
                    sm.company_id,
                    SUM(
                        sm.quantity * mu.factor / NULLIF(pu.factor, 0)
                        - COALESCE(consumed.qty, 0)
                    )                                                           AS fifo_remaining_qty,
                    SUM(
                        (sm.quantity * mu.factor / NULLIF(pu.factor, 0)
                            - COALESCE(consumed.qty, 0))
                        * CASE WHEN sm.quantity > 0
                               THEN sm.value
                                    / (sm.quantity * mu.factor / NULLIF(pu.factor, 0))
                               ELSE 0 END
                    )                                                           AS fifo_remaining_value
                FROM stock_move sm
                JOIN product_product pp ON pp.id = sm.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                JOIN uom_uom mu          ON mu.id = sm.product_uom
                JOIN uom_uom pu          ON pu.id = pt.uom_id
                LEFT JOIN (
                    SELECT fl.incoming_move_id, SUM(fl.quantity) AS qty
                    FROM odooer_fifo_link fl
                    GROUP BY fl.incoming_move_id
                ) consumed ON consumed.incoming_move_id = sm.id
                WHERE sm.is_in     = TRUE
                  AND sm.state     = 'done'
                  AND sm.quantity  > 0
                  AND sm.company_id IN ({company_ids})
                GROUP BY sm.product_id, sm.company_id
            ),

            -- On-hand in internal locations (excludes transit)
            onhand AS (
                SELECT sq.product_id, sq.company_id,
                       SUM(sq.quantity)          AS qty,
                       SUM(sq.reserved_quantity) AS reserved_qty
                FROM stock_quant sq
                JOIN stock_location sl ON sl.id = sq.location_id
                WHERE sl.usage = 'internal'
                  AND sq.company_id IN ({company_ids})
                GROUP BY sq.product_id, sq.company_id
            ),

            -- Qty in transit locations
            transit AS (
                SELECT sq.product_id, sq.company_id,
                       SUM(sq.quantity) AS qty
                FROM stock_quant sq
                JOIN stock_location sl ON sl.id = sq.location_id
                WHERE sl.usage = 'transit'
                  AND sq.company_id IN ({company_ids})
                GROUP BY sq.product_id, sq.company_id
            )
        """.format(company_ids=company_ids)

    def _select(self):
        return """
            -- Stable row id: hash of (product_id, company_id)
            (fs.product_id * 100000 + fs.company_id)                           AS id,
            fs.company_id,
            fs.product_id,
            pt.categ_id,
            pt.uom_id,

            -- FIFO unit cost = total remaining value / total remaining qty
            CASE WHEN fs.fifo_remaining_qty > 0
                 THEN fs.fifo_remaining_value / fs.fifo_remaining_qty
                 ELSE 0 END                                                     AS unit_cost,

            fs.fifo_remaining_qty,
            fs.fifo_remaining_value,

            -- On-hand (internal only)
            COALESCE(oh.qty, 0)                                                 AS onhand_qty,
            CASE WHEN fs.fifo_remaining_qty > 0
                 THEN fs.fifo_remaining_value / fs.fifo_remaining_qty
                 ELSE 0 END
                * COALESCE(oh.qty, 0)                                           AS onhand_value,

            -- Free to use
            COALESCE(oh.qty, 0) - COALESCE(oh.reserved_qty, 0)                 AS free_qty,

            -- In transit
            COALESCE(tr.qty, 0)                                                 AS transit_qty,
            CASE WHEN fs.fifo_remaining_qty > 0
                 THEN fs.fifo_remaining_value / fs.fifo_remaining_qty
                 ELSE 0 END
                * COALESCE(tr.qty, 0)                                           AS transit_value
        """

    def _from(self):
        return """
            fifo_summary fs
            JOIN product_product pp ON pp.id = fs.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
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
