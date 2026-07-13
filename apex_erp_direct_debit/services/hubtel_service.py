"""
HubtelService — Direct Mode implementation.

Calls Hubtel Pre-Approval API (mandate / pre-approval lifecycle) and
Hubtel Receive Money API (debit charge) directly from ERPNext.

Ported and adapted from KolectPay Business's HubtelDirectDebitService.php.

API Endpoints:
  Pre-Approval Base : https://preapproval.hubtel.com/api/v2/merchant
  Receive Money     : https://rmp.hubtel.com/merchantaccount/merchants
  Txn Status Check  : https://api-txnstatus.hubtel.com/transactions

Auth: HTTP Basic Auth (client_id : client_secret)
"""

import re
import uuid

import frappe
import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException

from apex_erp_direct_debit.services.base import DirectDebitProviderBase

# ─── Hubtel API base URLs ────────────────────────────────────────────────────
_PREAPPROVAL_BASE = "https://preapproval.hubtel.com/api/v2/merchant"
_RECEIVE_BASE = "https://rmp.hubtel.com/merchantaccount/merchants"
_STATUS_BASE = "https://api-txnstatus.hubtel.com/transactions"

# ─── Channel mappings (network prefix → Hubtel channel string) ────────────────
_CHANNEL_MAP = {
    "mtn":      "mtn-gh-direct-debit",
    "vodafone": "vodafone-gh-direct-debit",
    "voda":     "vodafone-gh-direct-debit",
    "airteltigo": "airteltigo-gh-direct-debit",
    "tigo":     "airteltigo-gh-direct-debit",
    "airtel":   "airteltigo-gh-direct-debit",
}


class HubtelService(DirectDebitProviderBase):
	"""
	Direct Hubtel Pre-Approval + Receive Money integration.

	MANDATE FLOW:
	  1. initiate_mandate()   → POST /preapproval/initiate
	     Response contains verificationType (USSD|OTP) and otpPrefix
	  2a. USSD: customer approves on phone — wait for mandate callback
	  2b. OTP: customer receives SMS → call verify_otp()
	             POST /preapproval/verifyotp
	  3. Hubtel POSTs mandate callback → webhook handler updates status
	  4. trigger_debit()      → POST receive/mobilemoney
	  5. Hubtel POSTs debit callback → webhook handler creates Payment Entry
	"""

	def __init__(self, settings):
		super().__init__(settings)
		self._client_id = settings.hubtel_client_id
		self._client_secret = settings.get_password("hubtel_client_secret")
		self._collection_account = settings.hubtel_collection_account
		self._timeout = 30
		self._connect_timeout = 10

	# ─────────────────────────────────────────────────────────────────────
	# STEP 1 — Initiate Mandate
	# ─────────────────────────────────────────────────────────────────────

	def initiate_mandate(self, mandate_doc) -> dict:
		"""
		POST {base}/{collectionAccount}/preapproval/initiate

		Generates a fresh clientReferenceId on every call — reusing the same
		ref causes Hubtel to treat it as a duplicate and suppress OTP/USSD.

		Updates the mandate document with the preapproval ID and OTP details.
		"""
		phone = mandate_doc.mobile_number_formatted
		if not phone:
			frappe.throw("Mandate has no formatted mobile number. Save the mandate first.")

		channel = self._resolve_channel(phone, mandate_doc.channel)
		client_ref = self._fresh_client_ref("MND")

		# Persist the ref so verify_otp and callbacks can find the mandate
		frappe.db.set_value("DD Mandate", mandate_doc.name, {
			"client_reference_id": client_ref,
			"source": "Hubtel",
		})
		mandate_doc.client_reference_id = client_ref

		callback_url = self._webhook_url("mandate-callback")
		payload = {
			"clientReferenceId": client_ref,
			"customerMsisdn":    phone,
			"channel":           channel,
			"callbackUrl":       callback_url,
		}

		frappe.logger("apex_dd").info(
			f"[Hubtel] initiate_mandate REQUEST | mandate={mandate_doc.name} | "
			f"phone={phone} | channel={channel} | ref={client_ref}"
		)

		response = self._post(
			f"{_PREAPPROVAL_BASE}/{self._collection_account}/preapproval/initiate",
			payload,
		)

		# Handle success (responseCode = "2000")
		if response.get("responseCode") == "2000":
			data = response.get("data", {})
			frappe.db.set_value("DD Mandate", mandate_doc.name, {
				"hubtel_preapproval_id": data.get("hubtelPreApprovalId", ""),
				"verification_type":     data.get("verificationType", ""),
				"otp_prefix":            data.get("otpPrefix", ""),
				"mandate_status":        "Pending",
				"metadata":              frappe.as_json(response),
			})

		frappe.logger("apex_dd").info(
			f"[Hubtel] initiate_mandate RESPONSE | mandate={mandate_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# STEP 2b — Verify OTP (OTP flow only)
	# ─────────────────────────────────────────────────────────────────────

	def verify_otp(self, mandate_doc, otp_code: str) -> dict:
		"""
		POST {base}/{collectionAccount}/preapproval/verifyotp

		Normalises OTP: if customer provides just "1234" and we have the prefix
		"HNRM" stored, it becomes "HNRM-1234" automatically.
		"""
		phone = mandate_doc.mobile_number_formatted
		preapproval_id = mandate_doc.hubtel_preapproval_id
		client_ref = mandate_doc.client_reference_id
		otp_prefix = mandate_doc.otp_prefix

		if not preapproval_id:
			frappe.throw("Mandate has no hubtel_preapproval_id. Did mandate initiation succeed?")

		# Normalise: if only 4 digits given, prepend the stored prefix
		otp_code = otp_code.strip()
		if otp_prefix and re.match(r"^\d{4}$", otp_code):
			otp_code = f"{otp_prefix.upper().strip()}-{otp_code}"

		payload = {
			"customerMsisdn":      phone,
			"hubtelPreApprovalId": preapproval_id,
			"clientReferenceId":   client_ref,
			"otpCode":             otp_code,
		}

		frappe.logger("apex_dd").info(
			f"[Hubtel] verify_otp REQUEST | mandate={mandate_doc.name} | otp={otp_code}"
		)

		response = self._post(
			f"{_PREAPPROVAL_BASE}/{self._collection_account}/preapproval/verifyotp",
			payload,
			timeout=20,
		)

		frappe.logger("apex_dd").info(
			f"[Hubtel] verify_otp RESPONSE | mandate={mandate_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# CANCEL
	# ─────────────────────────────────────────────────────────────────────

	def cancel_mandate(self, mandate_doc) -> dict:
		"""GET {base}/{collectionAccount}/preapproval/{phone}/cancel"""
		phone = mandate_doc.mobile_number_formatted
		frappe.logger("apex_dd").info(
			f"[Hubtel] cancel_mandate REQUEST | mandate={mandate_doc.name} | phone={phone}"
		)
		response = self._get(
			f"{_PREAPPROVAL_BASE}/{self._collection_account}/preapproval/{phone}/cancel",
			timeout=20,
		)
		if response.get("responseCode") == "2000":
			mandate_doc.mark_cancelled()
		frappe.logger("apex_dd").info(
			f"[Hubtel] cancel_mandate RESPONSE | mandate={mandate_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# REACTIVATE
	# ─────────────────────────────────────────────────────────────────────

	def reactivate_mandate(self, mandate_doc) -> dict:
		"""POST {base}/{collectionAccount}/preapproval/reactivate"""
		phone = mandate_doc.mobile_number_formatted
		payload = {
			"callbackUrl":    self._webhook_url("mandate-callback"),
			"customerMsisdn": phone,
		}
		frappe.logger("apex_dd").info(
			f"[Hubtel] reactivate_mandate REQUEST | mandate={mandate_doc.name}"
		)
		response = self._post(
			f"{_PREAPPROVAL_BASE}/{self._collection_account}/preapproval/reactivate",
			payload,
			timeout=30,
		)
		if response.get("responseCode") == "2000":
			data = response.get("data", {})
			frappe.db.set_value("DD Mandate", mandate_doc.name, {
				"hubtel_preapproval_id": data.get("hubtelPreApprovalId", mandate_doc.hubtel_preapproval_id),
				"verification_type":     data.get("verificationType", ""),
				"otp_prefix":            data.get("otpPrefix", ""),
				"mandate_status":        "Pending",
			})
		frappe.logger("apex_dd").info(
			f"[Hubtel] reactivate_mandate RESPONSE | mandate={mandate_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# STATUS CHECK
	# ─────────────────────────────────────────────────────────────────────

	def check_mandate_status(self, mandate_doc) -> dict:
		"""GET {base}/{collectionAccount}/preapproval/{clientRef}/status"""
		client_ref = mandate_doc.client_reference_id
		if not client_ref:
			frappe.throw("Mandate has no client_reference_id to check status.")
		response = self._get(
			f"{_PREAPPROVAL_BASE}/{self._collection_account}/preapproval/{client_ref}/status",
			timeout=15,
		)
		frappe.logger("apex_dd").info(
			f"[Hubtel] check_mandate_status | mandate={mandate_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# STEP 4 — Trigger Debit
	# ─────────────────────────────────────────────────────────────────────

	def trigger_debit(self, transaction_doc) -> dict:
		"""
		POST {receive_base}/{collectionAccount}/receive/mobilemoney

		Expected response: responseCode "0001" = accepted (pending).
		Final status arrives via debit callback webhook.

		IMPORTANT: clientReferenceId must be unique per transaction — never
		reuse a ref as Hubtel will reject as duplicate.
		"""
		debt = frappe.get_doc("DD Debt", transaction_doc.debt)
		mandate = frappe.get_doc("DD Mandate", transaction_doc.mandate)
		phone = mandate.mobile_number_formatted
		channel = self._resolve_channel(phone, mandate.channel)

		# Generate a fresh unique ref for this specific transaction
		client_ref = self._fresh_client_ref("TXN")
		frappe.db.set_value("DD Transaction", transaction_doc.name, "client_reference_id", client_ref)

		narration = (debt.narration or f"Installment collection - {debt.customer}")[:100]

		payload = {
			"Amount":             float(transaction_doc.amount),
			"CustomerMsisdn":     phone,
			"Channel":            channel,
			"ClientReference":    client_ref,
			"Description":        narration,
			"PrimaryCallbackUrl": self._webhook_url("debit-callback"),
		}

		frappe.logger("apex_dd").info(
			f"[Hubtel] trigger_debit REQUEST | txn={transaction_doc.name} | "
			f"amount={transaction_doc.amount} | ref={client_ref}"
		)

		response = self._post(
			f"{_RECEIVE_BASE}/{self._collection_account}/receive/mobilemoney",
			payload,
		)

		frappe.logger("apex_dd").info(
			f"[Hubtel] trigger_debit RESPONSE | txn={transaction_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# TRANSACTION STATUS CHECK
	# ─────────────────────────────────────────────────────────────────────

	def check_transaction_status(self, transaction_doc) -> dict:
		"""GET {status_base}/{clientRef}/status"""
		client_ref = transaction_doc.client_reference_id
		if not client_ref:
			frappe.throw(f"DD Transaction {transaction_doc.name} has no client_reference_id.")
		response = self._get(
			f"{_STATUS_BASE}/{client_ref}/status",
			timeout=15,
		)
		frappe.logger("apex_dd").info(
			f"[Hubtel] check_transaction_status | txn={transaction_doc.name} | {response}"
		)
		return response

	# ─────────────────────────────────────────────────────────────────────
	# Internal helpers
	# ─────────────────────────────────────────────────────────────────────

	def _auth(self):
		return HTTPBasicAuth(self._client_id, self._client_secret)

	def _post(self, url: str, payload: dict, timeout: int = None) -> dict:
		t = timeout or self._timeout
		try:
			resp = requests.post(
				url,
				json=payload,
				auth=self._auth(),
				timeout=(self._connect_timeout, t),
			)
			resp.raise_for_status()
			return resp.json() or {}
		except RequestException as e:
			frappe.log_error(title=f"[Hubtel] POST error: {url}", message=str(e))
			return {"error": str(e), "responseCode": "ERR"}

	def _get(self, url: str, timeout: int = None) -> dict:
		t = timeout or self._timeout
		try:
			resp = requests.get(
				url,
				auth=self._auth(),
				timeout=(self._connect_timeout, t),
			)
			resp.raise_for_status()
			return resp.json() or {}
		except RequestException as e:
			frappe.log_error(title=f"[Hubtel] GET error: {url}", message=str(e))
			return {"error": str(e), "responseCode": "ERR"}

	def _fresh_client_ref(self, prefix: str = "DD") -> str:
		"""
		Generate a unique, alphanumeric client reference ID.
		Hubtel constraint: max 36 chars, alphanumeric only, must be unique per transaction.
		"""
		uid = uuid.uuid4().hex[:24].upper()
		ref = f"{prefix}{uid}"
		return ref[:36]

	def _resolve_channel(self, phone: str, selected_channel: str = None) -> str:
		"""
		Map the selected ERPNext channel or phone prefix to a Hubtel channel string.
		Falls back to MTN if not recognised.
		"""
		if selected_channel:
			key = selected_channel.lower().replace(" ", "").replace("-", "")
			for k, v in _CHANNEL_MAP.items():
				if key.startswith(k):
					return v

		# Detect from phone prefix (Ghana numbers)
		digits = re.sub(r"\D", "", phone)
		if digits.startswith("233"):
			digits = "0" + digits[3:]
		prefixes_mtn =       ["024", "054", "055", "059"]
		prefixes_vodafone =  ["020", "050"]
		prefixes_airteltigo = ["027", "057", "026", "056", "023", "053"]

		for p in prefixes_mtn:
			if digits.startswith(p):
				return "mtn-gh-direct-debit"
		for p in prefixes_vodafone:
			if digits.startswith(p):
				return "vodafone-gh-direct-debit"
		for p in prefixes_airteltigo:
			if digits.startswith(p):
				return "airteltigo-gh-direct-debit"

		return "mtn-gh-direct-debit"  # safe default

	def _webhook_url(self, event: str) -> str:
		"""Build the Frappe webhook URL for a given Hubtel event type."""
		site_url = frappe.utils.get_url()
		return (
			f"{site_url}/api/method/apex_erp_direct_debit.api.webhook.handle_hubtel"
			f"?event={event}"
		)
