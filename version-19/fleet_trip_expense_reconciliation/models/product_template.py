from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    expense_cost_editable = fields.Boolean(
        string="Editable Unit Cost on Expenses", copy=False,
        help="Let users change the unit cost of this product on an expense (e.g. fuel prices that vary). "
             "The cost on the product itself is never changed.")
