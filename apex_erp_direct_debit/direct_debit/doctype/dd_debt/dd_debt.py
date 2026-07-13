import frappe
from frappe.model.document import Document
from frappe.utils import add_days, add_months, add_weeks, flt, now_datetime, today


class DDDebt(Document):
	"""
	A loan/collection agreement that generates an Installment Schedule
	and drives recurring debit operations against a DD Mandate.
	"""

	def validate(self):
		self._compute_installment_amount()
		self._compute_end_date()
		self._validate_mandate_approved()

	def before_insert(self):
		self._compute_installment_amount()
		self._compute_end_date()
		if not self.installment_schedule:
			self.generate_installment_schedule()
		self.outstanding_amount = self.total_amount
		self.total_collected = 0.0
		self.next_debit_date = self.start_date
		self._push_to_bridge()

	def after_insert(self):
		# Link this debt back to the mandate so the mandate knows it has an active debt
		if self.mandate:
			frappe.db.set_value("DD Mandate", self.mandate, "bridge_debt_id", self.bridge_debt_id or self.name)

	def _push_to_bridge(self):
		if not self.company:
			return
		if not frappe.db.exists("DD Settings", self.company):
			return
		settings = frappe.get_doc("DD Settings", self.company)
		if not settings.is_enabled:
			return
		if settings.integration_mode not in ("Bridge - KolectPay Business", "Bridge - SMCollect"):
			return
		if self.bridge_debt_id:
			return
		try:
			from apex_erp_direct_debit.services.provider_factory import get_provider
			provider = get_provider(self.company)
			res = provider.create_debt(self)
			bridge_id = res.get("id") or res.get("data", {}).get("id")
			if bridge_id:
				self.bridge_debt_id = str(bridge_id)
		except Exception:
			frappe.log_error(
				title=f"DD: Failed to push DD Debt {self.name} to bridge",
				message=frappe.get_traceback()
			)

	# ─────────────────────────────────────────────────────────────────────
	# Installment Schedule Generation
	# ─────────────────────────────────────────────────────────────────────

	def generate_installment_schedule(self):
		"""Build the installment_schedule child table rows from collection terms."""
		self.set("installment_schedule", [])
		if self.collection_type == "One-Time":
			self.append("installment_schedule", {
				"due_date": self.start_date,
				"installment_amount": self.total_amount,
				"status": "Pending",
				"retry_count": 0,
			})
		elif self.collection_type in ("Installment", "Subscription"):
			num = int(self.number_of_installments or 1)
			amount = flt(self.installment_amount)
			current_date = frappe.utils.getdate(self.start_date)
			for i in range(num):
				self.append("installment_schedule", {
					"due_date": str(current_date),
					"installment_amount": amount,
					"status": "Pending",
					"retry_count": 0,
				})
				current_date = self._next_date(current_date)

	def _next_date(self, current_date):
		freq = self.installment_frequency
		if freq == "Daily":
			return add_days(current_date, 1)
		elif freq == "Weekly":
			return add_weeks(current_date, 1)
		elif freq == "Monthly":
			return add_months(current_date, 1)
		return add_months(current_date, 1)

	# ─────────────────────────────────────────────────────────────────────
	# Financial Aggregation
	# ─────────────────────────────────────────────────────────────────────

	def recalculate_totals(self):
		"""Recompute outstanding_amount and total_collected from installment rows."""
		total_paid = flt(sum(
			flt(row.installment_amount)
			for row in self.installment_schedule
			if row.status == "Paid"
		))
		self.total_collected = total_paid
		self.outstanding_amount = flt(self.total_amount) - total_paid

		# Check if all installments are paid
		pending = [r for r in self.installment_schedule if r.status in ("Pending", "Failed", "Processing")]
		if not pending and total_paid >= flt(self.total_amount):
			self.debt_status = "Completed"

		# Advance next_debit_date to the earliest pending installment
		pending_sorted = sorted(
			[r for r in self.installment_schedule if r.status == "Pending"],
			key=lambda r: frappe.utils.getdate(r.due_date),
		)
		self.next_debit_date = pending_sorted[0].due_date if pending_sorted else None
		self.save(ignore_permissions=True)

	# ─────────────────────────────────────────────────────────────────────
	# Helpers
	# ─────────────────────────────────────────────────────────────────────

	def _compute_installment_amount(self):
		if self.collection_type == "Installment" and self.number_of_installments and self.total_amount:
			self.installment_amount = flt(self.total_amount) / int(self.number_of_installments)
		elif self.collection_type == "One-Time":
			self.installment_amount = self.total_amount

	def _compute_end_date(self):
		if self.collection_type == "One-Time":
			self.end_date = self.start_date
		elif self.collection_type in ("Installment", "Subscription"):
			num = int(self.number_of_installments or 1)
			end = frappe.utils.getdate(self.start_date)
			for _ in range(num - 1):
				end = self._next_date(end)
			self.end_date = str(end)

	def _validate_mandate_approved(self):
		if self.mandate and self.debt_status == "Active":
			status = frappe.db.get_value("DD Mandate", self.mandate, "mandate_status")
			if status != "Approved":
				frappe.throw(
					f"Mandate {self.mandate} must be in Approved status to activate a DD Debt. "
					f"Current status: {status}",
					title="Mandate Not Approved",
				)


@frappe.whitelist()
def bulk_pause_debts(names):
	names = frappe.parse_json(names)
	for name in names:
		doc = frappe.get_doc("DD Debt", name)
		if doc.debt_status == "Active":
			doc.debt_status = "Paused"
			doc.save(ignore_permissions=True)


@frappe.whitelist()
def bulk_resume_debts(names):
	names = frappe.parse_json(names)
	for name in names:
		doc = frappe.get_doc("DD Debt", name)
		if doc.debt_status == "Paused":
			doc.debt_status = "Active"
			doc.save(ignore_permissions=True)
