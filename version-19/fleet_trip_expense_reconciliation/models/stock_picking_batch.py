from odoo import api, fields, models

PARAM_THRESHOLD = 'mts_fleet_trip_reconciliation.low_loading_threshold'
ALERT_SUMMARY = "Low loading alert"  # untranslated on purpose: used to find/close the To-Do
TRIGGER_FIELDS = {'picking_ids', 'vehicle_id', 'vehicle_category_id'}


class StockPickingBatch(models.Model):
    _inherit = 'stock.picking.batch'

    low_loading = fields.Boolean(compute='_compute_low_loading', string="Low Loading")
    low_loading_message = fields.Char(compute='_compute_low_loading')
    low_loading_notified = fields.Boolean(copy=False, readonly=True)

    # ---------------- detection ----------------
    def _get_low_loading_threshold(self):
        try:
            return float(self.env['ir.config_parameter'].sudo().get_param(PARAM_THRESHOLD, '90'))
        except ValueError:
            return 90.0

    def _low_loading_message(self):
        """Alert text when weight OR volume use is below the threshold, else ''.
        A metric is ignored when the vehicle category has no capacity for it, or when no weight /
        volume is known yet (e.g. transfers not reserved)."""
        self.ensure_one()
        if not self.picking_ids or not self.vehicle_category_id or self.state in ('done', 'cancel'):
            return ''
        threshold = self._get_low_loading_threshold()
        low = []
        if self.vehicle_weight_capacity and self.estimated_shipping_weight \
                and self.used_weight_percentage < threshold:
            low.append(self.env._("Weight %(pct).1f%%", pct=self.used_weight_percentage))
        if self.vehicle_volume_capacity and self.estimated_shipping_volume \
                and self.used_volume_percentage < threshold:
            low.append(self.env._("Volume %(pct).1f%%", pct=self.used_volume_percentage))
        if not low:
            return ''
        return self.env._("Low loading: %(details)s - below %(t).0f%% of the vehicle capacity.",
                          details=", ".join(low), t=threshold)

    @api.depends('picking_ids', 'vehicle_category_id', 'state',
                 'estimated_shipping_weight', 'estimated_shipping_volume',
                 'used_weight_percentage', 'used_volume_percentage')
    def _compute_low_loading(self):
        for batch in self:
            msg = batch._low_loading_message()
            batch.low_loading = bool(msg)
            batch.low_loading_message = msg

    # ---------------- notification ----------------
    def _check_low_loading(self):
        """Create the alert (chatter note + To-Do) when a batch is low-loaded.
        Fires once per low-loading episode and re-arms when the load is corrected."""
        for batch in self.exists():
            msg = batch._low_loading_message()
            if msg and not batch.low_loading_notified:
                batch.message_post(body=msg, subtype_xmlid='mail.mt_note')
                user = batch.create_uid if batch.create_uid.active and not batch.create_uid.share else self.env.user
                batch.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=user.id,
                    summary=ALERT_SUMMARY, note=msg)
                batch.sudo().low_loading_notified = True
            elif not msg and batch.low_loading_notified:
                batch.activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY).sudo().unlink()
                batch.sudo().low_loading_notified = False

    @api.model_create_multi
    def create(self, vals_list):
        batches = super().create(vals_list)
        batches._check_low_loading()   # every new batch is checked
        return batches

    def write(self, vals):
        res = super().write(vals)
        if TRIGGER_FIELDS & set(vals):
            self._check_low_loading()
        return res

    def action_confirm(self):
        res = super().action_confirm()
        self._check_low_loading()  # weights are known once transfers are reserved
        return res


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    @api.model_create_multi
    def create(self, vals_list):
        pickings = super().create(vals_list)
        pickings.batch_id._check_low_loading()
        return pickings

    def write(self, vals):
        old_batches = self.batch_id if 'batch_id' in vals else self.env['stock.picking.batch']
        res = super().write(vals)
        if 'batch_id' in vals:
            (old_batches | self.batch_id)._check_low_loading()
        return res
