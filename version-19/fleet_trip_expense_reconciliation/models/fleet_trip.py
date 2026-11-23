from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

GUARDED_FIELDS = {'state', 'end_odometer', 'start_datetime', 'end_datetime'}
MANAGER_EDITABLE_FIELDS = {'end_odometer', 'start_datetime', 'end_datetime'}


class FleetTrip(models.Model):
    _name = 'fleet.trip'
    _description = 'Fleet Trip'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(string="Reference", default='New', copy=False, readonly=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company, required=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date = fields.Date(string="Trip Date", default=fields.Date.context_today, required=True, tracking=True)
    vehicle_id = fields.Many2one('fleet.vehicle', required=True, tracking=True)
    odometer_unit = fields.Selection(related='vehicle_id.odometer_unit')
    driver_id = fields.Many2one('hr.employee', string="Driver", tracking=True)
    manager_id = fields.Many2one(
        'res.users', string="Trip Manager", required=True, tracking=True,
        default=lambda s: s.env.user, domain=[('share', '=', False)],
        help="The only user allowed to start and end this trip.")
    state = fields.Selection(
        [('draft', 'Draft'), ('in_progress', 'In Progress'), ('done', 'Done'), ('cancelled', 'Cancelled')],
        default='draft', required=True, tracking=True, copy=False)
    is_manager = fields.Boolean(compute='_compute_is_manager')

    start_odometer = fields.Float(tracking=True)
    end_odometer = fields.Float(tracking=True, copy=False)
    start_datetime = fields.Datetime(copy=False, tracking=True)
    end_datetime = fields.Datetime(copy=False, tracking=True)
    distance = fields.Float(compute='_compute_distance', store=True)
    duration = fields.Float(string="Duration (h)", compute='_compute_distance', store=True)

    expense_ids = fields.One2many('hr.expense', 'trip_id', string="Other Expenses")
    fuel_line_ids = fields.One2many('fleet.trip.fuel', 'trip_id', string="Fuel Expenses")
    fuel_amount = fields.Monetary(string="Stock Fuel Cost", compute='_compute_fuel_amount', store=True,
                                  currency_field='currency_id')
    other_fuel_amount = fields.Monetary(string="Other Fuel Cost", compute='_compute_expense_totals', store=True,
                                        currency_field='currency_id')
    total_fuel_amount = fields.Monetary(string="Total Fuel Cost", compute='_compute_total_cost', store=True,
                                        currency_field='currency_id')
    other_fuel_expense_ids = fields.Many2many('hr.expense', compute='_compute_expense_line_ids',
                                              string="Other Fuel Expenses")
    other_fuel_count = fields.Integer(compute='_compute_expense_totals')
    total_cost = fields.Monetary(string="Total Trip Cost", compute='_compute_total_cost', store=True,
                                 currency_field='currency_id')
    # Read-only mirror of expense_ids used by the form. It always returns REAL records:
    # showing the One2many directly makes the web client build virtual (NewId) expenses
    # during onchange, which crashes hr_expense's _compute_same_receipt_expense_ids.
    expense_line_ids = fields.Many2many('hr.expense', compute='_compute_expense_line_ids', string="Other Expenses")
    expense_count = fields.Integer(compute='_compute_expense_totals')
    total_expense = fields.Monetary(string="Other Expenses Total", compute='_compute_expense_totals', store=True, currency_field='currency_id')
    company_paid_amount = fields.Monetary(compute='_compute_expense_totals', store=True, currency_field='currency_id')
    employee_paid_amount = fields.Monetary(compute='_compute_expense_totals', store=True, currency_field='currency_id')
    cost_per_distance = fields.Float(string="Cost per Distance", compute='_compute_total_cost', store=True, aggregator='avg')

    batch_ids = fields.Many2many('stock.picking.batch', string="Batch Transfers", copy=False)
    picking_ids = fields.Many2many('stock.picking', compute='_compute_pickings', string="Transfers")
    picking_count = fields.Integer(compute='_compute_pickings')
    notes = fields.Text()

    # ---------------- computes ----------------
    @api.depends('manager_id')
    @api.depends_context('uid')
    def _compute_is_manager(self):
        for trip in self:
            trip.is_manager = trip.manager_id == self.env.user

    @api.depends('start_odometer', 'end_odometer', 'start_datetime', 'end_datetime')
    def _compute_distance(self):
        for trip in self:
            trip.distance = max(trip.end_odometer - trip.start_odometer, 0.0) if trip.end_odometer else 0.0
            if trip.start_datetime and trip.end_datetime:
                trip.duration = (trip.end_datetime - trip.start_datetime).total_seconds() / 3600.0
            else:
                trip.duration = 0.0

    @api.depends('expense_ids', 'expense_ids.is_fuel_expense')
    def _compute_expense_line_ids(self):
        for trip in self:
            # sudo: is_fuel_expense is restricted to expense approvers, the Trip Manager may not be one
            expenses = trip._origin.expense_ids.sudo()
            fuel = expenses.filtered('is_fuel_expense')
            trip.other_fuel_expense_ids = fuel
            trip.expense_line_ids = expenses - fuel

    @api.depends('expense_ids.total_amount', 'expense_ids.state', 'expense_ids.payment_mode',
                 'expense_ids.is_fuel_expense')
    def _compute_expense_totals(self):
        for trip in self:
            all_expenses = trip._origin.expense_ids.sudo()   # real records only (see expense_line_ids)
            fuel = all_expenses.filtered('is_fuel_expense')
            expenses = all_expenses - fuel                    # fuel expenses are counted separately
            valid = expenses.filtered(lambda e: e.state != 'refused')
            company_paid = valid.filtered(lambda e: e.payment_mode == 'company_account')
            trip.expense_count = len(expenses)
            trip.total_expense = sum(valid.mapped('total_amount'))
            trip.company_paid_amount = sum(company_paid.mapped('total_amount'))
            trip.employee_paid_amount = trip.total_expense - trip.company_paid_amount
            trip.other_fuel_count = len(fuel)
            trip.other_fuel_amount = sum(fuel.filtered(lambda e: e.state != 'refused').mapped('total_amount'))

    @api.depends('fuel_line_ids.total_value')
    def _compute_fuel_amount(self):
        for trip in self:
            trip.fuel_amount = sum(trip.fuel_line_ids.mapped('total_value'))

    @api.depends('total_expense', 'fuel_amount', 'other_fuel_amount', 'distance')
    def _compute_total_cost(self):
        for trip in self:
            trip.total_fuel_amount = trip.fuel_amount + trip.other_fuel_amount
            trip.total_cost = trip.total_expense + trip.total_fuel_amount
            trip.cost_per_distance = trip.total_cost / trip.distance if trip.distance else 0.0

    @api.depends('batch_ids.picking_ids')
    def _compute_pickings(self):
        for trip in self:
            trip.picking_ids = trip.batch_ids.picking_ids
            trip.picking_count = len(trip.picking_ids)

    @api.onchange('vehicle_id')
    def _onchange_vehicle_id(self):
        if self.vehicle_id:
            self.start_odometer = self.vehicle_id.odometer
            if self.vehicle_id.driver_id and not self.driver_id:
                self.driver_id = self.env['hr.employee'].search(
                    [('work_contact_id', '=', self.vehicle_id.driver_id.id)], limit=1)

    # ---------------- constraints ----------------
    @api.constrains('vehicle_id', 'state')
    def _check_single_active_trip(self):
        for trip in self.filtered(lambda t: t.state == 'in_progress'):
            if self.search_count([('vehicle_id', '=', trip.vehicle_id.id),
                                  ('state', '=', 'in_progress'), ('id', '!=', trip.id)]):
                raise ValidationError(self.env._("Vehicle %s already has a trip in progress.",
                                                 trip.vehicle_id.display_name))

    @api.constrains('start_datetime', 'end_datetime')
    def _check_datetimes(self):
        for trip in self:
            if trip.start_datetime and trip.end_datetime and trip.end_datetime < trip.start_datetime:
                raise ValidationError(self.env._("The end time cannot be before the start time."))

    @api.constrains('batch_ids')
    def _check_batch_unique(self):
        for trip in self:
            clash = self.search([('batch_ids', 'in', trip.batch_ids.ids), ('id', '!=', trip.id),
                                 ('state', '!=', 'cancelled')])
            if clash and trip.batch_ids:
                raise ValidationError(self.env._(
                    "A batch transfer can only belong to one trip (already on %s).",
                    ", ".join(clash.mapped('name'))))

    # ---------------- ORM ----------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('fleet.trip') or 'New'
        return super().create(vals_list)

    def write(self, vals):
        if GUARDED_FIELDS & set(vals) and not self.env.context.get('trip_internal') and not self.env.su:
            # The Trip Manager may type the readings and timestamps while the trip is still open
            # (draft / in progress). Status changes always go through the buttons.
            manager_editable = (set(vals) & GUARDED_FIELDS) <= MANAGER_EDITABLE_FIELDS
            if not (manager_editable
                    and all(t.manager_id == self.env.user and t.state in ('draft', 'in_progress') for t in self)):
                raise UserError(self.env._(
                    "Only the Trip Manager can change the readings and times, and only while the trip "
                    "is not ended. The status changes through the Start / End Trip buttons."))
        return super().write(vals)

    def unlink(self):
        if any(t.state not in ('draft', 'cancelled') for t in self):
            raise UserError(self.env._("Only draft or cancelled trips can be deleted."))
        return super().unlink()

    # ---------------- actions ----------------
    def _check_is_manager(self):
        for trip in self:
            if trip.manager_id != self.env.user:
                raise UserError(self.env._(
                    "Only the Trip Manager (%(mgr)s) can do this on %(trip)s.",
                    mgr=trip.manager_id.name, trip=trip.name))

    def action_start(self):
        self._check_is_manager()
        for trip in self:
            if trip.state != 'draft':
                raise UserError(self.env._("Only draft trips can be started."))
            if not trip.start_odometer:
                raise UserError(self.env._("Enter the starting odometer reading first."))
            trip.with_context(trip_internal=True).write({
                'state': 'in_progress',
                'start_datetime': trip.start_datetime or fields.Datetime.now()})
        return True

    def action_end(self):
        self._check_is_manager()
        for trip in self:
            if trip.state != 'in_progress':
                raise UserError(self.env._("Only trips in progress can be ended."))
            if trip.end_odometer < trip.start_odometer:
                raise UserError(self.env._("The end odometer must be greater than or equal to the start reading."))
            trip._check_expenses_ready()
            trip._post_trip_expenses()
            trip.with_context(trip_internal=True).write({
                'state': 'done',
                'end_datetime': trip.end_datetime or fields.Datetime.now()})
            trip._log_odometer()
            trip._close_expense_activities()
        return True

    def action_cancel(self):
        self._check_is_manager()
        for trip in self:
            if trip.state == 'done':
                raise UserError(self.env._("A completed trip cannot be cancelled."))
            trip.with_context(trip_internal=True).write({'state': 'cancelled'})
        return True

    def action_reset_draft(self):
        self._check_is_manager()
        for trip in self.filtered(lambda t: t.state == 'cancelled'):
            trip.with_context(trip_internal=True).write({'state': 'draft'})
        return True

    # ---------------- expense handling ----------------
    def _check_expenses_ready(self):
        self.ensure_one()
        pending = self.expense_ids.sudo().filtered(lambda e: e.state in ('draft', 'submitted'))
        if pending:
            raise UserError(self.env._(
                "The trip cannot be ended while linked expenses are not approved:\n%s",
                "\n".join(f"- {e.name} ({e.employee_id.name}) - {e.state}" for e in pending)))

    def _post_trip_expenses(self):
        """Post approved expenses.
        Company-paid  -> action_post() (no popup).
        Employee-paid -> _post_without_wizard() (Odoo's private no-popup helper; isolated here so
        only this method needs review when upgrading)."""
        self.ensure_one()
        to_post = self.expense_ids.sudo().filtered(lambda e: e.state == 'approved')
        for company, expenses in to_post.grouped('company_id').items():
            expenses = expenses.with_company(company)
            company_paid = expenses.filtered(lambda e: e.payment_mode == 'company_account')
            own_account = expenses - company_paid
            if company_paid:
                company_paid.action_post()
            if own_account:
                own_account._post_without_wizard()

    def _expense_activity_summary(self, expense):
        return self.env._("Expense approved: %s", expense.name)

    def _schedule_expense_approved_activity(self, expense):
        """To-do for the Trip Manager (the assignment e-mail is sent by Odoo's activity
        notification, depending on the user's notification settings)."""
        self.ensure_one()
        summary = self._expense_activity_summary(expense)
        if self.activity_ids.filtered(lambda a: a.summary == summary):
            return
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            user_id=self.manager_id.id,
            summary=summary,
            note=self.env._(
                "%(exp)s (%(emp)s, %(amount)s) was approved and linked to trip %(trip)s. "
                "It will be posted automatically when the trip is ended.",
                exp=expense.name, emp=expense.employee_id.name,
                amount=expense.currency_id.format(expense.total_amount_currency), trip=self.name),
        )

    def _close_expense_activities(self):
        for trip in self:
            prefix = self.env._("Expense approved: ").split('%')[0]
            acts = trip.activity_ids.filtered(lambda a: a.summary and a.summary.startswith(prefix))
            acts.sudo().unlink()

    def _log_odometer(self):
        for trip in self:
            self.env['fleet.vehicle.odometer'].sudo().create({
                'vehicle_id': trip.vehicle_id.id,
                'value': trip.end_odometer,
                'date': fields.Date.context_today(trip),
            })

    # ---------------- smart buttons ----------------
    def action_view_expenses(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': self.env._("Other Expenses"),
                'res_model': 'hr.expense', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.expense_line_ids.ids)]}

    def action_view_fuel_expenses(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': self.env._("Other Fuel Expenses"),
                'res_model': 'hr.expense', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.other_fuel_expense_ids.ids)]}

    def action_view_pickings(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': self.env._("Transfers"),
                'res_model': 'stock.picking', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.picking_ids.ids)]}
