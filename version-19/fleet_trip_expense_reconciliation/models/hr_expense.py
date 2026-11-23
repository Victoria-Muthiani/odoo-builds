from odoo import api, fields, models
from odoo.exceptions import UserError

TRIP_APPROVER_GROUP = 'hr_expense.group_hr_expense_team_approver'


class HrExpense(models.Model):
    _inherit = 'hr.expense'

    # Only expense approvers can see / change these fields (field-level groups).
    is_trip_expense = fields.Boolean(
        string="Trip Expense", copy=False, tracking=True,
        groups=TRIP_APPROVER_GROUP,
        help="Tick to allow linking this expense to a fleet trip.")
    trip_id = fields.Many2one(
        'fleet.trip', string="Trip", copy=False, tracking=True, index='btree_not_null',
        groups=TRIP_APPROVER_GROUP, domain="[('state', 'in', ('draft', 'in_progress'))]")

    is_fuel_expense = fields.Boolean(
        string="Is a Fuel Expense", copy=False, tracking=True, groups=TRIP_APPROVER_GROUP,
        help="Show this expense under 'Other Fuel Expenses' on the trip and count it in the fuel cost.")

    @api.onchange('is_trip_expense')
    def _onchange_is_trip_expense(self):
        if not self.is_trip_expense:
            self.trip_id = False
            self.is_fuel_expense = False

    @api.constrains('is_fuel_expense', 'is_trip_expense')
    def _check_fuel_expense_is_trip_expense(self):
        for expense in self.sudo():
            if expense.is_fuel_expense and not expense.is_trip_expense:
                raise UserError(self.env._("'%s': only Trip Expenses can be marked as Fuel Expenses.", expense.name))

    def _check_trip_link_allowed(self, vals):
        if not ({'trip_id', 'is_trip_expense', 'is_fuel_expense'} & set(vals)):
            return
        for expense in self.sudo():
            if expense.trip_id.state == 'done':
                raise UserError(self.env._(
                    "Expense '%(exp)s' belongs to the completed trip %(trip)s and cannot be changed.",
                    exp=expense.name, trip=expense.trip_id.name))
            if expense.trip_id and expense.state in ('posted', 'in_payment', 'paid'):
                raise UserError(self.env._(
                    "Expense '%s' is already posted; its trip link cannot be changed.", expense.name))
            if vals.get('trip_id') and not vals.get('is_trip_expense', expense.is_trip_expense):
                raise UserError(self.env._("Tick 'Trip Expense' before choosing a trip."))
        if vals.get('trip_id'):
            new_trip = self.env['fleet.trip'].sudo().browse(vals['trip_id'])
            if new_trip.state in ('done', 'cancelled'):
                raise UserError(self.env._("You cannot link expenses to trip %(trip)s: it is %(state)s.",
                                           trip=new_trip.name, state=new_trip.state))

    def write(self, vals):
        self._check_trip_link_allowed(vals)
        if vals.get('is_trip_expense') is False and 'is_fuel_expense' not in vals:
            vals = {**vals, 'is_fuel_expense': False}
        res = super().write(vals)
        if vals.get('trip_id'):
            # Expense was already approved when the approver linked it -> notify now
            self.filtered(lambda e: e.state == 'approved')._notify_trip_manager()
        return res

    def _do_approve(self, check=True):
        """Notify the Trip Manager once a trip-linked expense gets approved."""
        before = self.sudo().filtered(lambda e: e.trip_id and e.state not in ('approved', 'posted', 'in_payment', 'paid'))
        res = super()._do_approve(check=check)
        before.filtered(lambda e: e.state == 'approved')._notify_trip_manager()
        return res

    def _notify_trip_manager(self):
        for expense in self.sudo():
            trip = expense.trip_id
            if trip and trip.state in ('draft', 'in_progress'):
                trip._schedule_expense_approved_activity(expense)
