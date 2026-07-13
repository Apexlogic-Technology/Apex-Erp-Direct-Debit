import frappe
from frappe.model.document import Document


class DDSettings(Document):
	"""
	Per-company configuration for Apex ERP Direct Debit.
	Named by company field — one record per ERPNext Company.
	"""

	def validate(self):
		self._validate_mode_credentials()
		self._validate_accounting()
		self.webhook_url = "/api/method/apex_erp_direct_debit.api.webhook.handle_hubtel"

	def _validate_mode_credentials(self):
		mode = self.integration_mode
		if mode == "Direct Mode":
			missing = []
			if not self.hubtel_client_id:
				missing.append("Hubtel Client ID")
			if not self.hubtel_client_secret:
				missing.append("Hubtel Client Secret")
			if not self.hubtel_collection_account:
				missing.append("Hubtel Collection Account")
			if missing:
				frappe.throw(
					f"The following fields are required for Direct Mode: {', '.join(missing)}",
					title="DD Settings — Missing Credentials",
				)
		elif mode == "KolectPay Mode":
			if not self.bridge_base_url:
				frappe.throw(
					"Bridge Base URL is required for KolectPay Mode.",
					title="DD Settings — Missing Bridge URL",
				)
			if not self.bridge_api_token:
				frappe.throw(
					"Bridge API Token is required for KolectPay Mode.",
					title="DD Settings — Missing Bridge Token",
				)

	def _validate_accounting(self):
		if self.auto_create_payment_entry:
			if not self.debit_account:
				frappe.throw(
					"Bank / Mobile Money Receipt Account is required when 'Auto-Create Payment Entry' is enabled.",
					title="DD Settings — Missing Account",
				)
			if not self.income_account:
				frappe.throw(
					"Receivable / Income Account is required when 'Auto-Create Payment Entry' is enabled.",
					title="DD Settings — Missing Account",
				)

	@staticmethod
	def get_for_company(company: str) -> "DDSettings | None":
		"""Return the DD Settings document for a given company, or None."""
		if not frappe.db.exists("DD Settings", company):
			return None
		doc = frappe.get_doc("DD Settings", company)
		if not doc.is_enabled:
			return None
		return doc
