from odoo import api, fields, models


class FleetTripFuelPostWizard(models.TransientModel):
    _name = 'fleet.trip.fuel.post.wizard'
    _description = 'Confirm Fuel Journal Entry'

    fuel_id = fields.Many2one('fleet.trip.fuel', required=True, readonly=True)
    trip_id = fields.Many2one(related='fuel_id.trip_id')
    product_id = fields.Many2one(related='fuel_id.product_id')
    quantity = fields.Float(related='fuel_id.quantity', digits='Product Unit')
    uom_id = fields.Many2one(related='fuel_id.uom_id')
    currency_id = fields.Many2one(related='fuel_id.currency_id')
    unit_cost = fields.Monetary(related='fuel_id.unit_cost', currency_field='currency_id')
    total_value = fields.Monetary(related='fuel_id.total_value', currency_field='currency_id')
    expense_account_id = fields.Many2one('account.account', string="Debit (Expense)", compute='_compute_accounts')
    stock_account_id = fields.Many2one('account.account', string="Credit (Stock)", compute='_compute_accounts')
    journal_id = fields.Many2one('account.journal', string="Journal", compute='_compute_accounts',
                                 store=False, readonly=False, domain="[('type', '=', 'general')]")
    date = fields.Date(string="Accounting Date", default=fields.Date.context_today, required=True)
    location_id = fields.Many2one(
        'stock.location', string="Issue Fuel From", required=True,
        domain="[('usage', '=', 'internal'), ('company_id', '=', company_id)]",
        default=lambda self: self._default_location_id())
    company_id = fields.Many2one(related='fuel_id.company_id')
    available_qty = fields.Float(string="On Hand Qty", compute='_compute_available_qty',
                                 digits='Product Unit')

    @api.model
    def _default_location_id(self):
        return self.env['stock.warehouse'].sudo().search(
            [('company_id', '=', self.env.company.id)], limit=1).lot_stock_id

    @api.depends('product_id', 'location_id')
    def _compute_available_qty(self):
        for wiz in self:
            wiz.available_qty = wiz.product_id.sudo().with_context(
                location=wiz.location_id.id).qty_available if wiz.location_id else 0.0

    @api.depends('fuel_id')
    def _compute_accounts(self):
        for wiz in self:
            expense, stock, journal = wiz.fuel_id._get_entry_accounts(raise_if_missing=False)
            wiz.expense_account_id = expense
            wiz.stock_account_id = stock
            wiz.journal_id = journal

    def action_confirm(self):
        self.ensure_one()
        self.fuel_id._post_entry(self.date, self.journal_id, self.location_id)
        return {'type': 'ir.actions.act_window_close'}
