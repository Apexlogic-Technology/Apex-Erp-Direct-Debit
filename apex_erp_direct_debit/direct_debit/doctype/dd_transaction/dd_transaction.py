import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class DDTransaction(Document):
	"""
	Records each individual debit attempt. Created by the scheduler or
	manual trigger. Updated by the webhook handler on callback.
	"""

	def mark_success(self, gateway_data: dict):
		self.status = "Success"
		self.gateway_txn_id = gateway_data.get("debitOrderTransactionId") or gateway_data.get("ClientTransactionId", "")
		self.network_txn_id = gateway_data.get("networkTransactionId", "")
		self.charge = gateway_data.get("charge", 0)
		self.settled_at = now_datetime()
		self.raw_callback = frappe.as_json(gateway_data)
		self.save(ignore_permissions=True)

	def mark_failed(self, reason: str, gateway_data: dict = None):
		self.status = "Failed"
		self.failure_reason = reason
		self.settled_at = now_datetime()
		if gateway_data:
			self.raw_callback = frappe.as_json(gateway_data)
		self.save(ignore_permissions=True)

	def mark_inconclusive(self, gateway_data: dict = None):
		self.status = "Inconclusive"
		if gateway_data:
			self.raw_callback = frappe.as_json(gateway_data)
		self.save(ignore_permissions=True)
