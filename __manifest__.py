# -*- coding: utf-8 -*-
{
    'name': 'Odooer Stock',
    'summary': 'FIFO cost-flow tracking and true gross profit reporting',
    'description': """
Odooer Stock
============
Standalone supplementary FIFO cost-flow layer for Odoo 19.

Key features:
- Links every outgoing move to the exact incoming receipts it consumed (FIFO)
- COGS (odooer_value) on outgoing moves updates dynamically when incoming
  move values change (bills posted, landed costs applied)
- True FIFO next-out cost on product (odooer_fifo_cost)
- Historical inventory valuation report using move dates
- Sale gross profit report: exact margin per sale line
- Works for ALL cost methods (Standard, AVCO, FIFO)
- Never touches GL/accounting — purely supplementary
- Company-level enable/disable flag
    """,
    'version': '19.0.1.0.0',
    'category': 'Inventory/Inventory',
    'license': 'LGPL-3',
    'author': 'chitswe',
    'website': 'https://github.com/odooer-io/odooer_stock',
    'depends': [
        'stock',
        'stock_account',
        'purchase_stock',
        'stock_landed_costs',
    ],
    'data': [
        'security/odooer_groups.xml',
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/odooer_inventory_valuation_views.xml',
        'views/odooer_fifo_regenerate_views.xml',
        'views/odooer_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
