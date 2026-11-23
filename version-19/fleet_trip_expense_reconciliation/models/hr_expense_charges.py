from odoo import Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

APPROVER_GROUP = 'hr_expense.group_hr_expense_team_approver'  # Team Approver, All Approver, Administrator
POSTED_STATES = ('posted', 'in_payment', 'paid')
LOCKED_FIELDS = {'has_transaction_charges', 'transaction_amount', 'transaction_account_id', 'mpesa_reference'}


class HrExpense(models.Model):
    _inherit = 'hr.expense'

    phone = fields.Char(string="Phone No/Till No/Paybill No", copy=False,
                        help="Phone number, Till number or Paybill number of the payee.")

    # Approver-only fields (field-level groups). Always read them through sudo() in code that
    # may run for non-approvers (posting, constraints).
    mpesa_reference = fields.Char(string="M-Pesa Reference", copy=False, tracking=True, groups=APPROVER_GROUP)
    has_transaction_charges = fields.Boolean(
        string="Has Transaction Charges", copy=False, tracking=True, groups=APPROVER_GROUP)
    transaction_amount = fields.Monetary(
        string="Transaction Amount", currency_field='currency_id', copy=False, tracking=True,
        groups=APPROVER_GROUP, help="Charge amount, in the currency of the expense.")
    transaction_account_id = fields.Many2one(
        'account.account', string="Transaction Charges Account", copy=False, tracking=True,
        groups=APPROVER_GROUP, domain="[('account_type', 'in', ('expense', 'expense_direct_cost'))]")

    # ---------------- UI helpers / validation ----------------
    @api.onchange('has_transaction_charges')
    def _onchange_has_transaction_charges(self):
        if not self.has_transaction_charges:
            self.transaction_amount = 0.0
            self.transaction_account_id = False

    @api.constrains('has_transaction_charges', 'transaction_amount', 'transaction_account_id')
    def _check_transaction_charges(self):
        for expense in self.sudo():
            if not expense.has_transaction_charges:
                continue
            if not expense.transaction_account_id:
                raise ValidationError(self.env._("Choose the account for the transaction charges of '%s'.", expense.name))
            if expense.currency_id.compare_amounts(expense.transaction_amount, 0.0) <= 0:
                raise ValidationError(self.env._("Enter a Transaction Amount greater than zero for '%s'.", expense.name))

    def write(self, vals):
        if LOCKED_FIELDS & set(vals) and not self.env.su:
            for expense in self.sudo():
                if expense.state in POSTED_STATES:
                    raise UserError(self.env._(
                        "'%s' is already posted: its M-Pesa reference and transaction charges can no longer be changed.",
                        expense.name))
        if vals.get('has_transaction_charges') is False:
            vals = {**vals, 'transaction_amount': 0.0, 'transaction_account_id': False}
        return super().write(vals)

    # ---------------- accounting: label ----------------
    def _get_move_line_name(self):
        """Append the M-Pesa reference to the label of every expense / payment journal item."""
        name = super()._get_move_line_name()
        reference = self.sudo().mpesa_reference
        return self.env._("%(name)s - M-Pesa %(ref)s", name=name, ref=reference) if reference else name

    def _get_posting_label(self):
        """Reference used on the payment memo / receipt reference."""
        self.ensure_one()
        reference = self.sudo().mpesa_reference
        return self.env._("%(name)s - M-Pesa %(ref)s", name=self.name, ref=reference) if reference else self.name

    # ---------------- accounting: transaction charges ----------------
    def _get_charge_amounts(self):
        """(amount in expense currency, balance in company currency) of the transaction charge."""
        self.ensure_one()
        expense = self.sudo()
        amount_currency = expense.currency_id.round(expense.transaction_amount)
        rate = expense.currency_rate or 1.0   # same rate as the expense: total_amount = total_amount_currency * rate
        balance = expense.company_currency_id.round(amount_currency * rate)
        return amount_currency, balance

    def _get_charge_label(self):
        self.ensure_one()
        return self.env._("%(label)s - Transaction charges", label=self._get_move_line_name())

    def _prepare_payments_vals(self):
        """Company-paid expenses: ONE payment whose entry has two separate outstanding lines:
            Dr expense account        329      Cr Outstanding payments   329   (expense label)
            Dr transaction charges      7      Cr Outstanding payments     7   (charge label)
        Each outstanding line can then be matched to its own bank statement line."""
        move_vals, payment_vals = super()._prepare_payments_vals()
        label = self._get_posting_label()
        if label != self.name:
            payment_vals['memo'] = label
            move_vals['ref'] = label
        expense = self.sudo()
        if expense.has_transaction_charges and expense.transaction_account_id:
            amount_currency, balance = self._get_charge_amounts()
            charge_label = self._get_charge_label()
            lines = move_vals['line_ids']
            lines.append(Command.create({
                'name': charge_label,
                'account_id': expense.transaction_account_id.id,
                # same analytic distribution as the expense -> same split of the charge
                'analytic_distribution': expense.analytic_distribution,
                'balance': balance,
                'amount_currency': amount_currency,
                'currency_id': self.currency_id.id,
                'partner_id': self.vendor_id.id,
            }))
            lines.append(Command.create({
                'name': charge_label,
                'account_id': self._get_expense_account_destination(),
                'balance': -balance,
                'amount_currency': -amount_currency,
                'currency_id': self.currency_id.id,
                'partner_id': self.vendor_id.id,
            }))
            payment_vals['amount'] = payment_vals['amount'] + amount_currency
        return move_vals, payment_vals

    def _prepare_receipts_vals(self):
        """Employee-paid expenses: add one charge line per expense to the receipt and the
        M-Pesa reference(s) to its reference."""
        vals_list = super()._prepare_receipts_vals()
        for vals in vals_list:
            expenses = self.sudo().browse(vals['expense_ids'][0][2])
            refs = [e.mpesa_reference for e in expenses if e.mpesa_reference]
            if refs:
                vals['ref'] = self.env._("%(ref)s - M-Pesa %(m)s", ref=vals.get('ref') or '', m=", ".join(refs))
            for expense in expenses.filtered(lambda e: e.has_transaction_charges and e.transaction_account_id):
                _amount_currency, balance = expense._get_charge_amounts()
                vals['line_ids'].append(Command.create({
                    'name': expense._get_charge_label(),
                    'account_id': expense.transaction_account_id.id,
                    'quantity': 1.0,
                    'price_unit': balance,            # receipts are in company currency
                    'tax_ids': [Command.clear()],
                    'analytic_distribution': expense.analytic_distribution,
                    'partner_id': expense.employee_id.sudo().work_contact_id.id,
                }))
        return vals_list
