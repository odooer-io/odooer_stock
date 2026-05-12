# -*- coding: utf-8 -*-
from odoo import fields, models


class OdooerOutgoingDetail(models.Model):
    """
    One row per odooer_fifo_link record — the outgoing moves that consumed
    a given incoming move's stock, shown in the valuation report popup.
    """
    _name = 'odooer.outgoing.detail'
    _description = 'Odooer Outgoing Detail'
    _auto = False
    _order = 'date'

    incoming_move_id = fields.Many2one('stock.move', string='Incoming Move', readonly=True)
    outgoing_move_id = fields.Many2one('stock.move', string='Outgoing Move', readonly=True)
    picking_id = fields.Many2one('stock.picking', string='Reference', readonly=True)
    date = fields.Date(string='Date', readonly=True)
    reference = fields.Char(string='Reference', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Partner', readonly=True)
    quantity = fields.Float(string='Qty', digits='Product Unit of Measure', readonly=True)
    unit_cost = fields.Float(string='Unit Cost', digits='Product Price', readonly=True)
    value = fields.Float(string='Value', digits='Product Price', readonly=True)

    @property
    def _table_query(self):
        return """
            SELECT
                fl.id                                                           AS id,
                fl.incoming_move_id                                             AS incoming_move_id,
                fl.outgoing_move_id                                             AS outgoing_move_id,
                out_sp.id                                                       AS picking_id,
                out_sm.date::date                                               AS date,
                COALESCE(out_sp.name, out_sm.reference)                         AS reference,
                out_sp.partner_id                                               AS partner_id,
                fl.quantity                                                     AS quantity,
                CASE WHEN COALESCE(sml_in.qty, 0) > 0
                     THEN in_sm.value / sml_in.qty
                     ELSE 0 END                                                 AS unit_cost,
                fl.quantity * CASE WHEN COALESCE(sml_in.qty, 0) > 0
                                   THEN in_sm.value / sml_in.qty
                                   ELSE 0 END                                  AS value
            FROM odooer_fifo_link fl
            JOIN stock_move  out_sm  ON out_sm.id  = fl.outgoing_move_id
            LEFT JOIN stock_picking out_sp ON out_sp.id = out_sm.picking_id
            JOIN stock_move  in_sm   ON in_sm.id   = fl.incoming_move_id
            LEFT JOIN (
                SELECT sml.move_id, SUM(sml.quantity_product_uom) AS qty
                FROM   stock_move_line sml
                WHERE  sml.state = 'done'
                GROUP  BY sml.move_id
            ) sml_in ON sml_in.move_id = in_sm.id
        """
