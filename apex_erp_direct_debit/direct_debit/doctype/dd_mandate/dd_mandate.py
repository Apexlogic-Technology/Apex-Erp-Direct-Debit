import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class DDMandate(Document):
	"""
	Represents a Hubtel Direct Debit mandate (pre-approval) for a Customer.
	One active mandate per Customer at a time (enforced in validate).
	"""

	def validate(self):
		self._format_mobile_number()
		self._prevent_duplicate_active()

	def before_save(self):
		self._format_mobile_number()

	# ─────────────────────────────────────────────────────────────────────
	# Helpers
	# ─────────────────────────────────────────────────────────────────────

	def _format_mobile_number(self):
		"""Normalise mobile number to 233XXXXXXXXX format for Hubtel API."""
		if not self.mobile_number:
			return
		digits = "".join(filter(str.isdigit, self.mobile_number))
		if digits.startswith("233"):
			self.mobile_number_formatted = digits
		elif digits.startswith("0") and len(digits) == 10:
			self.mobile_number_formatted = "233" + digits[1:]
		elif len(digits) == 9:
			self.mobile_number_formatted = "233" + digits
		else:
			self.mobile_number_formatted = digits

	def _prevent_duplicate_active(self):
		"""Warn if another active mandate exists for this customer."""
		if self.mandate_status not in ("Pending", "Approved"):
			return
		existing = frappe.db.get_value(
			"DD Mandate",
			{
				"customer": self.customer,
				"mandate_status": ["in", ["Pending", "Approved"]],
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if existing:
			frappe.msgprint(
				_(
					f"Customer {self.customer} already has an active mandate <b>{existing}</b>. "
					"Consider cancelling it before creating a new one."
				),
				title=_("Duplicate Active Mandate"),
				indicator="orange",
			)

	# ─────────────────────────────────────────────────────────────────────
	# Status transition helpers
	# ─────────────────────────────────────────────────────────────────────

	def mark_approved(self, hubtel_data: dict = None):
		from frappe.utils import add_months
		self.mandate_status = "Approved"
		self.approved_at = now_datetime()
		# Auto-set expiry to 12 months from approval (Hubtel standard)
		if not self.expires_at:
			self.expires_at = str(add_months(now_datetime(), 12))
		if hubtel_data:
			self.metadata = frappe.as_json(hubtel_data)
		self.save(ignore_permissions=True)

		# Sync back to Customer custom fields so the Customer form shows current status
		frappe.db.set_value("Customer", self.customer, {
			"dd_mandate":        self.name,
			"dd_mandate_status": "Approved",
		})

	def mark_failed(self, reason: str = None, hubtel_data: dict = None):
		self.mandate_status = "Failed"
		if hubtel_data:
			self.metadata = frappe.as_json(hubtel_data)
		self.save(ignore_permissions=True)
		frappe.log_error(
			title=f"DD Mandate {self.name} — Failed",
			message=reason or "Mandate marked failed via status update",
		)

	def mark_cancelled(self):
		self.mandate_status = "Cancelled"
		self.cancelled_at = now_datetime()
		self.save(ignore_permissions=True)

	def mark_expired(self):
		self.mandate_status = "Expired"
		self.save(ignore_permissions=True)

	@staticmethod
	def get_active_for_customer(customer: str) -> "DDMandate | None":
		"""Return the approved mandate for a customer, or None."""
		name = frappe.db.get_value(
			"DD Mandate",
			{"customer": customer, "mandate_status": "Approved"},
			"name",
		)
		return frappe.get_doc("DD Mandate", name) if name else None
