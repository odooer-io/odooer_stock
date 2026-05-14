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
        if (self._has_column('stock_move', 'purchase_line_id')
                and self._has_column('account_move_line', 'purchase_line_id')):
            parts.append("""
                SELECT
                    am.id::bigint * 10 + 2          AS id,
                    sm.id                            AS incoming_move_id,
                    'bill'                           AS source_type,
                    am.name                          AS reference,
                    am.id                            AS account_move_id,
                    NULL::integer                    AS landed_cost_id,
                    SUM(aml.balance)                         AS value,
                    comp.currency_id                 AS currency_id,
                    am.invoice_date::timestamp       AS date
                FROM stock_move sm
                JOIN purchase_order_line pol ON pol.id = sm.purchase_line_id
                JOIN account_move_line   aml ON aml.purchase_line_id = pol.id
                JOIN account_move        am  ON am.id = aml.move_id
                    AND am.state     = 'posted'
                    AND am.move_type IN ('in_invoice', 'in_refund')
                JOIN res_company comp ON comp.id = sm.company_id
                WHERE sm.is_in = TRUE
                GROUP BY am.id, am.name, am.invoice_date, sm.id, comp.currency_id
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
        return " UNION ALL ".join(f"({p})" for p in parts)
