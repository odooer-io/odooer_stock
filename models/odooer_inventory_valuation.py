# -*- coding: utf-8 -*-
from odoo import api, fields, models


class OdooerInventoryValuationLine(models.TransientModel):
    """One product row in the historical inventory valuation result."""
    _name = 'odooer.inventory.valuation.line'
    _description = 'Odooer Inventory Valuation Line'
    _order = 'categ_id, product_id'

    wizard_id = fields.Many2one(
        'odooer.inventory.valuation.wizard', ondelete='cascade',
    )
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    product_tmpl_id = fields.Many2one(
        related='product_id.product_tmpl_id', string='Product Template', store=True,
    )
    categ_id = fields.Many2one(
        related='product_id.categ_id', string='Category', store=True,
    )
    uom_id = fields.Many2one(
        related='product_id.uom_id', string='Unit of Measure',
    )
    cost_method = fields.Selection(
        selection=[
            ('standard', 'Standard Price'),
            ('fifo', 'First In First Out (FIFO)'),
            ('average', 'Average Cost (AVCO)'),
        ],
        string='Costing Method',
        compute='_compute_cost_method',
    )

    def _compute_cost_method(self):
        for line in self:
            line.cost_method = line.product_id.categ_id.property_cost_method or False
    qty_on_hand = fields.Float(
        string='Quantity On Hand',
        digits='Product Unit of Measure',
    )
    unit_cost = fields.Float(
        string='Unit Cost',
        digits='Product Price',
    )
    total_value = fields.Monetary(
        string='Total Value',
        currency_field='currency_id',
    )
    currency_id = fields.Many2one('res.currency')


class OdooerInventoryValuationWizard(models.TransientModel):
    """
    Historical inventory valuation snapshot.

    Uses odooer.fifo.link records to compute the exact quantity and value
    of each product's stock at any past date.  The result is deterministic:
    it does not float with future price changes because we use move.value
    as it exists today (settled by bills and landed costs) but attribute it
    to the move's date.

    Invariant verified:
      For each incoming move dated <= to_date:
        remaining_qty = move.quantity - Σ(fifo_link.quantity WHERE outgoing.date <= to_date)
        remaining_value = remaining_qty × (move.value / move.quantity)
      inventory_value = Σ(remaining_value across all incoming moves)
    """
    _name = 'odooer.inventory.valuation.wizard'
    _description = 'Odooer Historical Inventory Valuation'

    to_date = fields.Date(
        string='Valuation Date',
        required=True,
        default=lambda self: fields.Date.context_today(self),
        help="Compute inventory value as of the end of this date. "
             "Uses stock move dates for attribution (not bill or landed cost dates).",
    )
    company_id = fields.Many2one(
        'res.company', string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', string='Currency',
    )
    line_ids = fields.One2many(
        'odooer.inventory.valuation.line', 'wizard_id',
        string='Valuation Lines',
    )
    total_value = fields.Monetary(
        string='Total Inventory Value',
        compute='_compute_total_value',
        currency_field='currency_id',
    )
    line_count = fields.Integer(compute='_compute_total_value')

    @api.depends('line_ids.total_value')
    def _compute_total_value(self):
        for wiz in self:
            wiz.total_value = sum(wiz.line_ids.mapped('total_value'))
            wiz.line_count = len(wiz.line_ids)

    def action_compute(self):
        """Run the SQL valuation query and populate line_ids."""
        self.ensure_one()
        self.line_ids.unlink()

        rows = self._fetch_valuation_rows()
        if not rows:
            return

        currency = self.company_id.currency_id
        lines = []
        for row in rows:
            qty = row['qty']
            value = row['value']
            unit_cost = value / qty if qty else 0.0
            lines.append({
                'wizard_id': self.id,
                'product_id': row['product_id'],
                'qty_on_hand': qty,
                'unit_cost': unit_cost,
                'total_value': value,
                'currency_id': currency.id,
            })
        self.env['odooer.inventory.valuation.line'].create(lines)

        # Return the same wizard view (refreshed)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _fetch_valuation_rows(self):
        """
        SQL: for each incoming move dated <= to_date, compute:
          remaining_qty  = move.quantity - Σ(consumed by outgoing moves dated <= to_date)
          remaining_value = remaining_qty × (move.value / move.quantity)
        Then group by product_id summing qty and value.

        Returns list of dicts with keys: product_id, qty, value.
        """
        self.env.cr.execute("""
            SELECT
                sm.product_id                                           AS product_id,
                SUM(
                    (sm.quantity - COALESCE(consumed.qty, 0.0))
                )                                                       AS qty,
                SUM(
                    (sm.quantity - COALESCE(consumed.qty, 0.0))
                    * (sm.value / NULLIF(sm.quantity, 0))
                )                                                       AS value
            FROM stock_move sm
            LEFT JOIN (
                SELECT
                    fl.incoming_move_id,
                    SUM(fl.quantity) AS qty
                FROM odooer_fifo_link fl
                INNER JOIN stock_move out_sm
                    ON out_sm.id = fl.outgoing_move_id
                WHERE out_sm.date::date <= %(to_date)s
                GROUP BY fl.incoming_move_id
            ) consumed ON consumed.incoming_move_id = sm.id
            WHERE sm.is_in         = true
              AND sm.state         = 'done'
              AND sm.date::date   <= %(to_date)s
              AND sm.company_id    = %(company_id)s
              AND sm.quantity      > 0
            GROUP BY sm.product_id
            HAVING SUM(sm.quantity - COALESCE(consumed.qty, 0.0)) > 0.000001
            ORDER BY sm.product_id
        """, {
            'to_date': self.to_date,
            'company_id': self.company_id.id,
        })
        return self.env.cr.dictfetchall()
