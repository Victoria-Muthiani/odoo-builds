from odoo import api, fields, models
from odoo.tools import float_compare


class HrExpense(models.Model):
    _inherit = 'hr.expense'

    # Make the field writable at model level; the form view decides when it is really editable.
    price_unit = fields.Float(readonly=False)
    unit_cost_overridden = fields.Boolean(
        copy=False, readonly=True,
        help="The unit cost was changed on this expense. The product's cost is not affected.")
    can_edit_unit_cost = fields.Boolean(compute='_compute_can_edit_unit_cost')

    @api.depends_context('uid')
    @api.depends('state', 'is_editable', 'product_has_cost', 'product_id.expense_cost_editable')
    def _compute_can_edit_unit_cost(self):
        for expense in self:
            expense.can_edit_unit_cost = bool(
                expense.state == 'draft' and expense.is_editable and expense.product_has_cost
                and expense.product_id.expense_cost_editable)

    # ---------------- keep the edited cost ----------------
    @api.depends('total_amount', 'total_amount_currency')
    def _compute_price_unit(self):
        """Odoo resets the unit price to the product cost whenever the totals change. Skip that
        for expenses whose cost was edited, so the new cost survives until submission/approval."""
        kept = self.filtered(lambda e: e.unit_cost_overridden and e.state == 'draft')
        super(HrExpense, self - kept)._compute_price_unit()

    def _product_standard_cost(self, product, company=None, uom=None):
        company = company or self.company_id or self.env.company
        return product._price_compute('standard_price', uom=uom or product.uom_id, company=company)[product.id]

    def _is_other_cost(self, new_cost, product_cost, company):
        return bool(float_compare(new_cost, product_cost, precision_rounding=company.currency_id.rounding))

    @api.onchange('price_unit')
    def _onchange_price_unit_override(self):
        for expense in self:
            if expense.state == 'draft' and expense.product_id.expense_cost_editable and expense.product_has_cost:
                cost = expense._product_standard_cost(expense.product_id, expense.company_id, expense.product_uom_id)
                expense.unit_cost_overridden = expense._is_other_cost(
                    expense.price_unit, cost, expense.company_id or self.env.company)

    @api.onchange('product_id')
    def _onchange_product_reset_cost_override(self):
        for expense in self:
            expense.unit_cost_overridden = False

    # ---------------- chatter log ----------------
    def _log_unit_cost_change(self, old_cost, new_cost, product_cost):
        self.ensure_one()
        currency = self.company_currency_id
        body = self.env._(
            "Unit cost changed by %(user)s: %(old)s -> %(new)s (cost on the product %(product)s stays %(cost)s).",
            user=self.env.user.name, old=currency.format(old_cost), new=currency.format(new_cost),
            product=self.product_id.display_name, cost=currency.format(product_cost))
        self.message_post(body=body, subtype_xmlid='mail.mt_note')

    @api.model_create_multi
    def create(self, vals_list):
        pending = []
        for index, vals in enumerate(vals_list):
            product = self.env['product.product'].browse(vals.get('product_id'))
            if product and 'price_unit' in vals and product.expense_cost_editable:
                company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
                uom = self.env['uom.uom'].browse(vals.get('product_uom_id')) or product.uom_id
                cost = self._product_standard_cost(product, company, uom)
                if self._is_other_cost(vals['price_unit'], cost, company):
                    vals['unit_cost_overridden'] = True   # set BEFORE create so the cost is never reset
                    pending.append((index, cost, vals['price_unit']))
        expenses = super().create(vals_list)
        for index, cost, new_cost in pending:
            expenses[index]._log_unit_cost_change(cost, new_cost, cost)
        return expenses

    def write(self, vals):
        if 'price_unit' in vals and len(self) > 1:
            for expense in self:
                expense.write(vals)
            return True
        log = None
        if 'product_id' in vals and 'price_unit' not in vals and 'unit_cost_overridden' not in vals:
            vals = {**vals, 'unit_cost_overridden': False}
        elif 'price_unit' in vals and len(self) == 1 and self.state == 'draft':
            product = self.env['product.product'].browse(vals['product_id']) if vals.get('product_id') \
                else self.product_id
            if product.expense_cost_editable:
                company = self.company_id or self.env.company
                cost = self._product_standard_cost(product, company, self.product_uom_id)
                overridden = self._is_other_cost(vals['price_unit'], cost, company)
                vals = {**vals, 'unit_cost_overridden': overridden}
                old = self.price_unit
                if self._is_other_cost(vals['price_unit'], old, company):
                    log = (old, vals['price_unit'], cost)
        res = super().write(vals)
        if log:
            self._log_unit_cost_change(*log)
        return res
