# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerValuationSource(models.Model):
    """
    Value-source breakdown for one incoming stock move.

    Shows every record that contributes to sm.value:
      - manual   : product.value record linked to the move
      - bill      : posted vendor bill(s) via purchase_line_id
      - production: manufacturing order (mrp_production)
      - quotation : PO price when not yet billed
      - return    : value from the original outgoing move
      - landed_cost: stock_valuation_adjustment_lines (0..N per move)

    One row per source record; landed_costs are additional rows on top
    of the base source row(s).
    """
    _name = 'odooer.valuation.source'
    _description = 'Odooer Valuation Source'
    _auto = False
    _order = 'incoming_move_id, source_type, date'

    incoming_move_id = fields.Many2one('stock.move', string='Incoming Move', readonly=True)
    source_type = fields.Selection([
        ('manual',       'Manual Adjustment'),
        ('bill',         'Vendor Bill'),
        ('quotation',    'PO Quotation'),
        ('return',       'Sale Return'),
        ('landed_cost',  'Landed Cost'),
    ], string='Source', readonly=True)
    reference = fields.Char(string='Reference', readonly=True)
    account_move_id = fields.Many2one('account.move', string='Bill / LC Bill', readonly=True)
    landed_cost_id = fields.Many2one('stock.landed.cost', string='Landed Cost', readonly=True)
    value = fields.Monetary(string='Value', currency_field='currency_id', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    date = fields.Datetime(string='Date', readonly=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _has_table(self, table):
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=%s", (table,))
        return bool(self.env.cr.fetchone())

    def _has_column(self, table, column):
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name=%s AND column_name=%s", (table, column))
        return bool(self.env.cr.fetchone())

    # ── query parts ──────────────────────────────────────────────────────────

    def _parts(self):
        parts = []

        # ── 1. Manual overrides (product.value linked to a move) ─────────────
        if self._has_table('product_value'):
            parts.append("""
                SELECT
                    pv.id::bigint * 10 + 1          AS id,
                    pv.move_id                       AS incoming_move_id,
                    'manual'                         AS source_type,
                    'Adjusted by ' || rp.name        AS reference,
                    NULL::integer                    AS account_move_id,
                    NULL::integer                    AS landed_cost_id,
                    pv.value                         AS value,
                    comp.currency_id                 AS currency_id,
                    pv.date                          AS date
                FROM product_value pv
                JOIN res_users  ru   ON ru.id  = pv.user_id
                JOIN res_partner rp  ON rp.id  = ru.partner_id
                JOIN stock_move  sm  ON sm.id  = pv.move_id
                JOIN res_company comp ON comp.id = sm.company_id
                WHERE pv.move_id IS NOT NULL
            """)

        # ── 2. Vendor bills via purchase_line_id ─────────────────────────────
        # Uses cumulative-qty ranges to attribute bill amounts per receipt move.
        # When a PO line has multiple receipts and multiple bills, each receipt
        # only sees the bill(s) whose billed-qty range overlaps its received-qty
        # range (ordered by date). This matches how Odoo sets sm.value.
        if (self._has_column('stock_move', 'purchase_line_id')
                and self._has_column('account_move_line', 'purchase_line_id')):
            parts.append("""
                WITH _bill_qtys AS (
                    SELECT
                        aml.purchase_line_id                AS pol_id,
                        am.id                               AS am_id,
                        am.name                             AS am_name,
                        am.invoice_date,
                        SUM(ABS(aml.quantity))              AS bill_qty,
                        SUM(aml.balance)                    AS bill_balance
                    FROM account_move_line aml
                    JOIN account_move am ON am.id = aml.move_id
                        AND am.state     = 'posted'
                        AND am.move_type IN ('in_invoice', 'in_refund')
                    WHERE aml.purchase_line_id IS NOT NULL
                    GROUP BY aml.purchase_line_id, am.id, am.name, am.invoice_date
                ),
                _bill_cum AS (
                    SELECT *,
                        SUM(bill_qty) OVER (
                            PARTITION BY pol_id ORDER BY invoice_date, am_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        )              AS cum_bill_end,
                        SUM(bill_qty) OVER (
                            PARTITION BY pol_id ORDER BY invoice_date, am_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) - bill_qty   AS cum_bill_start
                    FROM _bill_qtys
                ),
                _move_qtys AS (
                    SELECT
                        sm.purchase_line_id                 AS pol_id,
                        sm.id                               AS sm_id,
                        sm.date,
                        SUM(sml.quantity_product_uom)       AS move_qty
                    FROM stock_move sm
                    JOIN stock_move_line sml ON sml.move_id = sm.id AND sml.state = 'done'
                    WHERE sm.is_in = TRUE AND sm.state = 'done'
                      AND sm.purchase_line_id IS NOT NULL
                    GROUP BY sm.purchase_line_id, sm.id, sm.date
                ),
                _move_cum AS (
                    SELECT *,
                        SUM(move_qty) OVER (
                            PARTITION BY pol_id ORDER BY date, sm_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        )              AS cum_move_end,
                        SUM(move_qty) OVER (
                            PARTITION BY pol_id ORDER BY date, sm_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) - move_qty   AS cum_move_start
                    FROM _move_qtys
                )
                SELECT
                    bc.am_id::bigint * 10 + 2                       AS id,
                    mc.sm_id                                         AS incoming_move_id,
                    'bill'                                           AS source_type,
                    bc.am_name                                       AS reference,
                    bc.am_id                                         AS account_move_id,
                    NULL::integer                                    AS landed_cost_id,
                    bc.bill_balance
                        * (LEAST(bc.cum_bill_end, mc.cum_move_end)
                           - GREATEST(bc.cum_bill_start, mc.cum_move_start))
                        / NULLIF(bc.bill_qty, 0)                    AS value,
                    comp.currency_id                                 AS currency_id,
                    bc.invoice_date::timestamp                       AS date
                FROM _move_cum mc
                JOIN _bill_cum bc ON bc.pol_id = mc.pol_id
                    AND bc.cum_bill_end   > mc.cum_move_start
                    AND bc.cum_bill_start < mc.cum_move_end
                JOIN stock_move sm ON sm.id = mc.sm_id
                JOIN res_company comp ON comp.id = sm.company_id
            """)

        # ── 3. Landed costs ───────────────────────────────────────────────────
        if (self._has_table('stock_valuation_adjustment_lines')
                and self._has_table('stock_landed_cost')):
            parts.append("""
                SELECT
                    sval.id::bigint * 10 + 3        AS id,
                    sval.move_id                     AS incoming_move_id,
                    'landed_cost'                    AS source_type,
                    COALESCE(vb.name, lc.name)       AS reference,
                    vb.id                            AS account_move_id,
                    lc.id                            AS landed_cost_id,
                    sval.additional_landed_cost      AS value,
                    curr.id                          AS currency_id,
                    lc.date::timestamp               AS date
                FROM stock_valuation_adjustment_lines sval
                JOIN stock_landed_cost lc   ON lc.id  = sval.cost_id AND lc.state = 'done'
                JOIN res_company       comp ON comp.id = lc.company_id
                JOIN res_currency      curr ON curr.id = comp.currency_id
                LEFT JOIN account_move vb  ON vb.id  = lc.vendor_bill_id
                WHERE sval.move_id IS NOT NULL
            """)

        # ── 4. PO quotation (purchase_line exists, no posted bill yet) ────────
        if (self._has_column('stock_move', 'purchase_line_id')
                and self._has_table('purchase_order')):
            no_bill_guard = ""
            if self._has_column('account_move_line', 'purchase_line_id'):
                no_bill_guard = """
                    AND NOT EXISTS (
                        SELECT 1
                        FROM account_move_line aml2
                        JOIN account_move am2 ON am2.id = aml2.move_id
                        WHERE aml2.purchase_line_id = sm.purchase_line_id
                          AND am2.state     = 'posted'
                          AND am2.move_type = 'in_invoice'
                    )"""
            parts.append(f"""
                SELECT
                    sm.id::bigint * 10 + 4           AS id,
                    sm.id                             AS incoming_move_id,
                    'quotation'                       AS source_type,
                    po.name                           AS reference,
                    NULL::integer                     AS account_move_id,
                    NULL::integer                     AS landed_cost_id,
                    pol.price_unit * COALESCE(sml_qty.qty, 0) AS value,
                    comp.currency_id                  AS currency_id,
                    sm.date                           AS date
                FROM stock_move sm
                JOIN purchase_order_line pol ON pol.id = sm.purchase_line_id
                JOIN purchase_order       po  ON po.id  = pol.order_id
                JOIN res_company         comp ON comp.id = sm.company_id
                LEFT JOIN (
                    SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                    FROM   stock_move_line sml
                    WHERE  sml.state = 'done'
                    GROUP  BY sml.move_id
                ) sml_qty ON sml_qty.move_id = sm.id
                WHERE sm.is_in = TRUE
                  AND sm.purchase_line_id IS NOT NULL
                  {no_bill_guard}
            """)

        # ── 5. Sale return (value from original outgoing move) ────────────────
        parts.append("""
            SELECT
                sm.id::bigint * 10 + 5              AS id,
                sm.id                                AS incoming_move_id,
                'return'                             AS source_type,
                orig.reference                       AS reference,
                NULL::integer                        AS account_move_id,
                NULL::integer                        AS landed_cost_id,
                orig.value * COALESCE(sml_qty.qty, 0)
                    / NULLIF(orig_sml.qty, 0)        AS value,
                comp.currency_id                     AS currency_id,
                sm.date                              AS date
            FROM stock_move sm
            JOIN stock_move  orig     ON orig.id  = sm.origin_returned_move_id
                                      AND orig.is_out = TRUE
            JOIN res_company comp     ON comp.id  = sm.company_id
            LEFT JOIN (
                SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                FROM   stock_move_line sml WHERE sml.state = 'done'
                GROUP  BY sml.move_id
            ) sml_qty  ON sml_qty.move_id  = sm.id
            LEFT JOIN (
                SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                FROM   stock_move_line sml WHERE sml.state = 'done'
                GROUP  BY sml.move_id
            ) orig_sml ON orig_sml.move_id = orig.id
            WHERE sm.is_in = TRUE AND sm.origin_returned_move_id IS NOT NULL
        """)

        return parts

    @property
    def _table_query(self):
        parts = self._parts()
        union = " UNION ALL ".join(f"({p})" for p in parts)
        # Wrap in ROW_NUMBER() to guarantee unique IDs across all source rows.
        # The raw parts use source_id * 10 + type, which collides when the same
        # bill (am_id) contributes to multiple receipt moves after the
        # cumulative-qty attribution join.
        return f"""
            SELECT
                ROW_NUMBER() OVER (
                    ORDER BY incoming_move_id, source_type, date
                ) AS id,
                incoming_move_id,
                source_type,
                reference,
                account_move_id,
                landed_cost_id,
                value,
                currency_id,
                date
            FROM ({union}) _src
        """
