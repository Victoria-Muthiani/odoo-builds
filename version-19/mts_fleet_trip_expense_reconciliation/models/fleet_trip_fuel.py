from odoo import Command, api, fields, models
from odoo.exceptions import UserError, ValidationError


class FleetTripFuel(models.Model):
    _name = 'fleet.trip.fuel'
    _description = 'Fleet Trip Fuel Line'
    _order = 'id'

    trip_id = fields.Many2one('fleet.trip', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='trip_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    product_id = fields.Many2one(
        'product.product', string="Fuel Product", required=True,
        domain="[('is_storable', '=', True)]")
    quantity = fields.Float(string="Quantity Issued", digits='Product Unit', default=1.0, required=True)
    uom_id = fields.Many2one(related='product_id.uom_id', string="UoM")
    unit_cost = fields.Monetary(
        string="Unit Cost", compute='_compute_unit_cost', store=True, readonly=True, precompute=True,
        currency_field='currency_id', help="Cost of the product when it was selected (not editable).")
    total_value = fields.Monetary(
        string="Total Value", compute='_compute_total_value', store=True, currency_field='currency_id')
    state = fields.Selection([('draft', 'To Post'), ('posted', 'Posted')],
                             default='draft', readonly=True, copy=False)
    move_id = fields.Many2one('account.move', string="Journal Entry", readonly=True, copy=False)
    scrap_id = fields.Many2one('stock.scrap', string="Stock Issue", readonly=True, copy=False)
    location_id = fields.Many2one('stock.location', string="Issued From", readonly=True, copy=False)

    # ---------------- computes / constraints ----------------
    @api.depends('product_id', 'company_id')
    def _compute_unit_cost(self):
        for line in self:
            line.unit_cost = line.product_id.with_company(line.company_id).standard_price

    @api.depends('quantity', 'unit_cost')
    def _compute_total_value(self):
        for line in self:
            line.total_value = line.quantity * line.unit_cost

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(self.env._("The quantity issued must be greater than zero."))

    # ---------------- access guards (Trip Manager only) ----------------
    def _check_trip_manager(self, trips):
        if self.env.su:
            return
        for trip in trips:
            if trip.manager_id != self.env.user:
                raise UserError(self.env._(
                    "Only the Trip Manager (%(mgr)s) can manage the fuel of %(trip)s.",
                    mgr=trip.manager_id.name, trip=trip.name))

    def _check_editable(self):
        for line in self:
            line._check_trip_manager(line.trip_id)
            if line.trip_id.state not in ('draft', 'in_progress'):
                raise UserError(self.env._("Fuel lines cannot be changed once the trip is ended or cancelled."))
            if line.state == 'posted':
                raise UserError(self.env._("A posted fuel line cannot be changed or deleted."))

    @api.model_create_multi
    def create(self, vals_list):
        trips = self.env['fleet.trip'].browse([v['trip_id'] for v in vals_list if v.get('trip_id')])
        self._check_trip_manager(trips)
        for trip in trips:
            if trip.state not in ('draft', 'in_progress'):
                raise UserError(self.env._("Fuel lines cannot be added once the trip is ended or cancelled."))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) & {'product_id', 'quantity', 'trip_id'} and not self.env.context.get('fuel_internal'):
            self._check_editable()
        return super().write(vals)

    def unlink(self):
        self._check_editable()
        return super().unlink()

    # ---------------- accounting ----------------
    def _get_entry_accounts(self, raise_if_missing=True):
        """Return (expense_account, stock_account, journal) configured for the product."""
        self.ensure_one()
        tmpl = self.product_id.product_tmpl_id.with_company(self.company_id)
        accounts = tmpl._get_product_accounts()
        expense = accounts.get('expense')
        stock = accounts.get('stock_valuation')
        journal = tmpl.get_product_accounts().get('stock_journal') or self.env['account.journal'].search(
            [('type', '=', 'general'), ('company_id', '=', self.company_id.id)], limit=1)
        if raise_if_missing:
            if not expense:
                raise UserError(self.env._("No expense account is set on %s or its category.",
                                           self.product_id.display_name))
            if not stock:
                raise UserError(self.env._("No stock valuation account is set on the category of %s.",
                                           self.product_id.display_name))
            if not journal:
                raise UserError(self.env._("No stock/miscellaneous journal found for %s.",
                                           self.company_id.name))
        return expense, stock, journal

    def _check_can_post(self):
        for line in self:
            line._check_trip_manager(line.trip_id)
            if line.trip_id.state == 'cancelled':
                raise UserError(self.env._("The trip is cancelled."))
            if line.state == 'posted':
                raise UserError(self.env._("This fuel line is already posted."))
            if line.company_id.currency_id.is_zero(line.total_value):
                raise UserError(self.env._("The total value is zero. Check the cost of %s.",
                                           line.product_id.display_name))

    def action_open_post_wizard(self):
        self.ensure_one()
        self._check_can_post()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Issue Fuel & Post Entry"),
            'res_model': 'fleet.trip.fuel.post.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_fuel_id': self.id},
        }

    def _issue_stock(self, location):
        """Move the fuel out of `location` into the fuel-consumption location (reduces on-hand)."""
        self.ensure_one()
        consumption = self.env.ref('mts_fleet_trip_reconciliation.location_fleet_fuel_consumption')
        scrap = self.env['stock.scrap'].sudo().with_company(self.company_id).create({
            'product_id': self.product_id.id,
            'scrap_qty': self.quantity,
            'product_uom_id': self.product_id.uom_id.id,
            'location_id': location.id,
            'scrap_location_id': consumption.id,
            'origin': self.trip_id.name,
            'company_id': self.company_id.id,
        })
        if not scrap.check_available_qty():
            raise UserError(self.env._(
                "Not enough stock of %(product)s in %(location)s to issue %(qty)s %(uom)s.",
                product=self.product_id.display_name, location=location.display_name,
                qty=self.quantity, uom=self.uom_id.name))
        scrap.do_scrap()
        return scrap

    def _post_entry(self, date, journal, location):
        """1) Issue the quantity from stock, 2) debit the product's expense account and credit its
        stock valuation account for the value actually taken out of stock."""
        self.ensure_one()
        self._check_can_post()
        if self.product_id.valuation != 'real_time':
            raise UserError(self.env._(
                "%s must belong to a product category with Automated inventory valuation.",
                self.product_id.display_name))
        expense, stock, default_journal = self._get_entry_accounts()
        journal = journal or default_journal

        scrap = self._issue_stock(location)
        stock_moves = scrap.move_ids
        # Value really taken out of stock (FIFO/AVCO aware); fall back to the line value
        value = abs(sum(stock_moves.mapped('value'))) or self.total_value
        label = self.env._("Fuel issued - %(trip)s - %(product)s (%(qty)s)",
                           trip=self.trip_id.name, product=self.product_id.display_name, qty=self.quantity)

        move = stock_moves.account_move_id[:1]
        if not move:
            # Normal case: the consumption location has no valuation account, so Odoo posted nothing.
            move = self.env['account.move'].sudo().with_company(self.company_id).create({
                'move_type': 'entry',
                'journal_id': journal.id,
                'date': date,
                'ref': label,
                'line_ids': [
                    Command.create({'account_id': expense.id, 'name': label, 'debit': value, 'credit': 0.0}),
                    Command.create({'account_id': stock.id, 'name': label, 'debit': 0.0, 'credit': value}),
                ],
            })
            move.action_post()
        # else: someone set a valuation account on the location -> Odoo already booked the entry;
        # we link it instead of posting a second one.

        self.with_context(fuel_internal=True).write({
            'state': 'posted', 'move_id': move.id, 'scrap_id': scrap.id, 'location_id': location.id,
            'unit_cost': value / self.quantity if self.quantity else self.unit_cost,
        })
        self.trip_id.message_post(body=self.env._(
            "Fuel issued: %(product)s, %(qty)s %(uom)s from %(loc)s (%(move)s)",
            product=self.product_id.display_name, qty=self.quantity,
            uom=self.uom_id.name, loc=location.display_name, move=move.name))
        return move

    def action_view_move(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'res_id': self.move_id.id, 'view_mode': 'form', 'target': 'current'}
