"""
SMCollectBridge — Bridge Mode implementation for SMCollect.

Mirrors KolectPayBridge but targets SMCollect's External API.
SMCollect supports both Hubtel and ITC — the specific gateway is
transparent to this bridge client.
"""

import frappe
import requests
from requests.exceptions import RequestException

from apex_erp_direct_debit.services.base import DirectDebitProviderBase


class SMCollectBridge(DirectDebitProviderBase):
	"""Bridge to SMCollect REST API."""

	def __init__(self, settings):
		super().__init__(settings)
		self._base_url = settings.bridge_base_url.rstrip("/")
		self._token = settings.get_password("bridge_api_token")
		self._timeout = int(settings.bridge_timeout_seconds or 30)

	def initiate_mandate(self, mandate_doc) -> dict:
		return self._post(f"/api/external/debts/{mandate_doc.bridge_debt_id}/initiate", {
			"callbackUrl": self._webhook_url(),
		})

	def verify_otp(self, mandate_doc, otp_code: str) -> dict:
		return self._post(f"/api/external/debts/{mandate_doc.bridge_debt_id}/verify-otp", {
			"otp": otp_code,
		})

	def cancel_mandate(self, mandate_doc) -> dict:
		return self._post(f"/api/external/debts/{mandate_doc.bridge_debt_id}/cancel", {})

	def reactivate_mandate(self, mandate_doc) -> dict:
		return self._post(f"/api/external/debts/{mandate_doc.bridge_debt_id}/reactivate", {})

	def check_mandate_status(self, mandate_doc) -> dict:
		return self._get(f"/api/external/debts/{mandate_doc.bridge_debt_id}")

	def trigger_debit(self, transaction_doc) -> dict:
		debt_doc = frappe.get_doc("DD Debt", transaction_doc.debt)
		return self._post(f"/api/external/debts/{debt_doc.bridge_debt_id}/trigger-debit", {
			"amount":    float(transaction_doc.amount),
			"reference": transaction_doc.name,
		})

	def check_transaction_status(self, transaction_doc) -> dict:
		return self._get(f"/api/external/transactions/{transaction_doc.gateway_txn_id}")

	def create_debtor(self, customer_doc) -> dict:
		return self._post("/api/external/debtors", {
			"name":  customer_doc.customer_name,
			"phone": customer_doc.dd_mobile_number,
			"email": customer_doc.email_id or "",
			"erpnext_id": customer_doc.name,
		})

	def create_debt(self, debt_doc) -> dict:
		debtor_id = frappe.db.get_value("Customer", debt_doc.customer, "dd_bridge_debtor_id")
		if not debtor_id:
			customer_doc = frappe.get_doc("Customer", debt_doc.customer)
			res = self.create_debtor(customer_doc)
			debtor_id = res.get("id") or res.get("data", {}).get("id")
			if not debtor_id:
				frappe.throw(f"Customer {debt_doc.customer} is not registered on the Bridge, and auto-registration failed.")
			frappe.db.set_value("Customer", debt_doc.customer, "dd_bridge_debtor_id", str(debtor_id))

		return self._post("/api/external/debts", {
			"debtor_id":       int(debtor_id),
			"total_amount":    float(debt_doc.total_amount),
			"description":     debt_doc.description or "",
			"reference":       debt_doc.name,
			"erpnext_debt_id": debt_doc.name,
		})

	def sync_transactions(self, company: str, since_datetime: str = None) -> list:
		params = {}
		if since_datetime:
			params["since"] = since_datetime
		response = self._get("/api/external/transactions", params=params)
		return response.get("data", []) if isinstance(response, dict) else []

	def _headers(self):
		return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

	def _post(self, path: str, payload: dict) -> dict:
		url = f"{self._base_url}{path}"
		try:
			resp = requests.post(url, json=payload, headers=self._headers(), timeout=self._timeout)
			resp.raise_for_status()
			return resp.json() or {}
		except RequestException as e:
			frappe.log_error(title=f"[SMCollect Bridge] POST error: {path}", message=str(e))
			return {"error": str(e)}

	def _get(self, path: str, params: dict = None) -> dict:
		url = f"{self._base_url}{path}"
		try:
			resp = requests.get(url, headers=self._headers(), params=params or {}, timeout=self._timeout)
			resp.raise_for_status()
			return resp.json() or {}
		except RequestException as e:
			frappe.log_error(title=f"[SMCollect Bridge] GET error: {path}", message=str(e))
			return {"error": str(e)}

	def _webhook_url(self) -> str:
		site_url = frappe.utils.get_url()
		return f"{site_url}/api/method/apex_erp_direct_debit.api.webhook.handle_hubtel"
