"""
Hubtel Webhook Handler.

Endpoint: POST /api/method/apex_erp_direct_debit.api.webhook.handle_hubtel

Security:
  1. Raw payload is ALWAYS logged to DD Webhook Log before any other processing.
  2. HMAC-SHA256 signature is verified if hubtel_webhook_secret is configured.
  3. IP allowlist is checked if hubtel_allowed_ips is configured.

Event types detected:
  - Mandate callback  : payload contains "PreapprovalStatus" key
  - Debit callback    : payload contains "ResponseCode" + "Amount" / "debitAmount"
"""

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import now_datetime, flt


@frappe.whitelist(allow_guest=True)
def handle_hubtel():
	"""
	Main webhook entry point. Accepts POST from Hubtel.
	Security checks (HMAC / IP) run first and return 403 on failure.
	Processing errors are logged internally and always return 200 (to prevent
	Hubtel from infinitely retrying on application bugs).
	"""
	try:
		raw_body = frappe.local.request.get_data(as_text=True)
		payload = json.loads(raw_body) if raw_body else {}
	except Exception:
		payload = {}
		raw_body = ""

	# ── Step 1: Log raw payload unconditionally ────────────────────────────
	log_name = _log_webhook(payload, raw_body)

	# ── Step 2: Security — runs OUTSIDE the processing try/except ──────────
	# Auth failures raise frappe.AuthenticationError which Frappe converts to
	# HTTP 401/403, correctly rejecting spoofed payloads before any DB writes.
	company = _detect_company(payload)
	if company and frappe.db.exists("DD Settings", company):
		settings = frappe.get_doc("DD Settings", company)
		_verify_ip(settings)
		_verify_hmac(settings, raw_body)

	# ── Step 3: Process payload (errors logged, always 200) ────────────────
	try:
		if _is_mandate_callback(payload):
			_handle_mandate_callback(payload, log_name)
		elif _is_debit_callback(payload):
			_handle_debit_callback(payload, log_name)
		else:
			frappe.log_error(
				title="DD Webhook: Unrecognised payload",
				message=json.dumps(payload, indent=2),
			)

		# Mark log as processed
		frappe.db.set_value("DD Webhook Log", log_name, "processed", 1)

	except Exception as exc:
		frappe.log_error(
			title="DD Webhook: Processing error",
			message=frappe.get_traceback(),
		)
		frappe.db.set_value("DD Webhook Log", log_name, "processing_error", str(exc))

	# Always return 200 OK for processing errors (never for auth failures)
	frappe.response["http_status_code"] = 200
	return {"status": "received"}


# ─── Mandate Callback ─────────────────────────────────────────────────────────

def _handle_mandate_callback(payload: dict, log_name: str):
	"""
	Hubtel sends this when a mandate is approved or failed.
	Expected fields: PreapprovalStatus, ClientReferenceId, Data.HubtelPreApprovalId
	"""
	status = (
		payload.get("PreapprovalStatus")
		or payload.get("preapprovalStatus")
		or ""
	).upper()

	# Find the mandate by clientReferenceId
	client_ref = (
		payload.get("Data", {}).get("ClientReferenceId")
		or payload.get("ClientReferenceId")
		or payload.get("clientReferenceId")
		or ""
	)

	mandate_name = frappe.db.get_value(
		"DD Mandate",
		{"client_reference_id": client_ref},
		"name",
	)

	if not mandate_name:
		frappe.log_error(
			title=f"DD Mandate Callback: mandate not found for ref '{client_ref}'",
			message=json.dumps(payload, indent=2),
		)
		return

	mandate = frappe.get_doc("DD Mandate", mandate_name)

	if status == "APPROVED":
		mandate.mark_approved(hubtel_data=payload)
		# Activate linked DD Debt if present
		_activate_debts_for_mandate(mandate_name)
		frappe.logger("apex_dd").info(f"[Webhook] Mandate {mandate_name} APPROVED")

	elif status in ("FAILED", "EXPIRED"):
		mandate.mark_failed(reason=f"Hubtel callback: {status}", hubtel_data=payload)
		frappe.logger("apex_dd").info(f"[Webhook] Mandate {mandate_name} {status}")

	# Update event type in log
	frappe.db.set_value("DD Webhook Log", log_name, "event_type", "mandate_callback")


def _activate_debts_for_mandate(mandate_name: str):
	"""Set linked Draft DD Debts to Active when mandate is approved."""
	debts = frappe.get_all(
		"DD Debt",
		filters={"mandate": mandate_name, "debt_status": "Draft"},
		pluck="name",
	)
	for debt_name in debts:
		frappe.db.set_value("DD Debt", debt_name, "debt_status", "Active")
		frappe.logger("apex_dd").info(f"[Webhook] Activated DD Debt {debt_name}")


# ─── Debit Callback ───────────────────────────────────────────────────────────

def _handle_debit_callback(payload: dict, log_name: str):
	"""
	Hubtel sends this after a debit charge completes (success or failure).
	ResponseCode "0000" = success. Others = failed/inconclusive.
	"""
	client_ref = (
		payload.get("ClientReference")
		or payload.get("clientReference")
		or payload.get("ClientReferenceId")
		or ""
	)
	response_code = (
		payload.get("ResponseCode")
		or payload.get("responseCode")
		or ""
	)
	amount = flt(payload.get("Amount") or payload.get("amount") or 0)
	gateway_txn_id = (
		payload.get("TransactionId")
		or payload.get("ClientTransactionId")
		or payload.get("transactionId")
		or ""
	)

	# Find DD Transaction by client reference (set during trigger_debit)
	txn_name = frappe.db.get_value(
		"DD Transaction",
		{"client_reference_id": client_ref},
		"name",
	)

	if not txn_name:
		frappe.log_error(
			title=f"DD Debit Callback: transaction not found for ref '{client_ref}'",
			message=json.dumps(payload, indent=2),
		)
		return

	txn = frappe.get_doc("DD Transaction", txn_name)
	frappe.db.set_value("DD Webhook Log", log_name, {
		"dd_transaction": txn_name,
		"event_type": "debit_callback",
	})

	if response_code == "0000":
		# ── SUCCESS ────────────────────────────────────────────────────────
		txn.mark_success(payload)
		_mark_installment_paid(txn)
		_create_payment_entry_if_enabled(txn, amount)
		_update_debt_totals(txn.debt)
		frappe.logger("apex_dd").info(
			f"[Webhook] Debit SUCCESS | txn={txn_name} | amount={amount} | ref={client_ref}"
		)

	elif response_code in ("111", "131"):
		# Inconclusive — will be re-checked by poll_pending_transactions
		txn.mark_inconclusive(payload)
		frappe.logger("apex_dd").info(f"[Webhook] Debit INCONCLUSIVE | txn={txn_name}")

	else:
		# Failed
		reason = payload.get("Message") or payload.get("message") or f"ResponseCode: {response_code}"
		txn.mark_failed(reason=reason, gateway_data=payload)
		_mark_installment_failed(txn, reason)
		frappe.logger("apex_dd").info(f"[Webhook] Debit FAILED | txn={txn_name} | reason={reason}")

		# ── Failure Alert Notification ────────────────────────────────────
		try:
			debt = frappe.get_doc("DD Debt", txn.debt)
			settings = frappe.get_doc("DD Settings", txn.company)
			if settings.send_failure_alerts:
				mandate = frappe.get_doc("DD Mandate", txn.mandate)
				phone = mandate.mobile_number_formatted or ""
				retry_days = int(settings.retry_interval_days or 1)
				channel = "WhatsApp" if settings.send_whatsapp_reminders else "SMS"
				from apex_erp_direct_debit.tasks import _send_failure_alert
				_send_failure_alert(
					customer=debt.customer,
					phone=phone,
					company=txn.company,
					amount=flt(txn.amount),
					retry_in_days=retry_days,
					channel=channel,
				)
		except Exception:
			frappe.log_error(title="DD: failure alert send error", message=frappe.get_traceback())


def _mark_installment_paid(txn):
	if not txn.installment_row:
		return
	frappe.db.set_value("DD Installment Schedule", txn.installment_row, {
		"status":       "Paid",
		"payment_entry": "",  # filled after PE creation
		"paid_on":      now_datetime(),
		"dd_transaction": txn.name,
	})


def _mark_installment_failed(txn, reason: str):
	if not txn.installment_row:
		return
	frappe.db.set_value("DD Installment Schedule", txn.installment_row, {
		"status":         "Failed",
		"failure_reason": reason[:140],
		"dd_transaction":  txn.name,
	})
	# Increment retry count
	current = frappe.db.get_value("DD Installment Schedule", txn.installment_row, "retry_count") or 0
	frappe.db.set_value("DD Installment Schedule", txn.installment_row, "retry_count", current + 1)


def _create_payment_entry_if_enabled(txn, amount: float):
	"""
	Create an ERPNext Payment Entry against the Sales Invoice, which
	automatically reduces the invoice's outstanding_amount.
	"""
	debt = frappe.get_doc("DD Debt", txn.debt)
	settings = frappe.get_doc("DD Settings", debt.company)

	if not settings.auto_create_payment_entry:
		return
	if not settings.debit_account or not settings.income_account:
		frappe.log_error(
			title="DD: Cannot create Payment Entry — accounts not configured",
			message=f"DD Debt: {debt.name}",
		)
		return

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = debt.company
	pe.posting_date = frappe.utils.today()
	pe.mode_of_payment = settings.mode_of_payment or "Mobile Money - Direct Debit"
	pe.party_type = "Customer"
	pe.party = debt.customer
	pe.paid_to = settings.debit_account
	pe.paid_from = settings.income_account
	pe.paid_amount = amount
	pe.received_amount = amount
	pe.reference_no = txn.client_reference_id or txn.gateway_txn_id or txn.name
	pe.reference_date = frappe.utils.today()
	pe.remarks = (
		f"Direct Debit collection via {settings.integration_mode} | "
		f"DD Transaction: {txn.name}"
	)

	# Link to Sales Invoice if present
	if debt.sales_invoice:
		outstanding = frappe.db.get_value("Sales Invoice", debt.sales_invoice, "outstanding_amount")
		alloc_amount = min(flt(amount), flt(outstanding or 0))
		if alloc_amount > 0:
			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name":    debt.sales_invoice,
				"allocated_amount":  alloc_amount,
			})

	pe.insert(ignore_permissions=True)
	pe.submit()

	# Link back to transaction and installment row
	frappe.db.set_value("DD Transaction", txn.name, "payment_entry", pe.name)
	frappe.db.set_value("DD Transaction", txn.name, "reconciled", 1)
	if txn.installment_row:
		frappe.db.set_value("DD Installment Schedule", txn.installment_row, "payment_entry", pe.name)

	frappe.logger("apex_dd").info(
		f"[Webhook] Created Payment Entry {pe.name} for DD Transaction {txn.name}"
	)


def _update_debt_totals(debt_name: str):
	"""Recompute outstanding/collected on the parent DD Debt."""
	debt = frappe.get_doc("DD Debt", debt_name)
	debt.recalculate_totals()


# ─── Security helpers ─────────────────────────────────────────────────────────

def _verify_hmac(settings, raw_body: str):
	"""
	Verify HMAC-SHA256 signature from Hubtel.
	Hubtel sends: X-Hubtel-Signature: sha256=<hex>
	"""
	secret = settings.get_password("hubtel_webhook_secret") if settings.hubtel_webhook_secret else None
	if not secret:
		return  # HMAC check disabled

	sig_header = frappe.local.request.headers.get("X-Hubtel-Signature", "")
	if not sig_header.startswith("sha256="):
		frappe.throw(_("Missing webhook signature."), frappe.AuthenticationError)

	expected = hmac.new(
		secret.encode("utf-8"),
		raw_body.encode("utf-8"),
		hashlib.sha256,
	).hexdigest()

	received = sig_header.replace("sha256=", "")
	if not hmac.compare_digest(expected, received):
		frappe.throw(_("Webhook signature mismatch."), frappe.AuthenticationError)


def _verify_ip(settings):
	"""Verify source IP against hubtel_allowed_ips allowlist."""
	allowed_raw = (settings.hubtel_allowed_ips or "").strip()
	if not allowed_raw:
		return  # IP check disabled
	allowed = [ip.strip() for ip in allowed_raw.split(",") if ip.strip()]
	remote_ip = frappe.local.request.remote_addr
	if remote_ip not in allowed:
		frappe.throw(
			_(f"Webhook rejected: IP {remote_ip} not in allowlist."),
			frappe.AuthenticationError,
		)


def _detect_company(payload: dict) -> str | None:
	"""
	Try to detect the ERPNext Company from payload context.
	Falls back to single-company site if only one DD Settings exists.
	"""
	# Try to match via client reference → DD Transaction → DD Debt → company
	for ref_key in ("ClientReference", "clientReference", "ClientReferenceId", "clientReferenceId"):
		ref = payload.get(ref_key)
		if ref:
			company = frappe.db.get_value(
				"DD Transaction", {"client_reference_id": ref}, "company"
			)
			if company:
				return company
			# Try mandate lookup
			company = frappe.db.get_value(
				"DD Mandate", {"client_reference_id": ref}, "company"
			)
			if company:
				return company

	# Single-settings fallback
	all_settings = frappe.get_all("DD Settings", filters={"is_enabled": 1}, pluck="name")
	if len(all_settings) == 1:
		return frappe.db.get_value("DD Settings", all_settings[0], "company")
	return None


# ─── Log helper ───────────────────────────────────────────────────────────────

def _log_webhook(payload: dict, raw_body: str) -> str:
	"""Always write the raw payload before any other processing."""
	log = frappe.get_doc({
		"doctype":     "DD Webhook Log",
		"event_type":  "unknown",
		"source":      "Hubtel",
		"received_at": now_datetime(),
		"processed":   0,
		"payload":     frappe.as_json(payload),
	})
	log.insert(ignore_permissions=True)
	frappe.db.commit()
	return log.name


# ─── Payload type detection ───────────────────────────────────────────────────

def _is_mandate_callback(payload: dict) -> bool:
	return bool(
		payload.get("PreapprovalStatus")
		or payload.get("preapprovalStatus")
	)


def _is_debit_callback(payload: dict) -> bool:
	return bool(
		(payload.get("ResponseCode") or payload.get("responseCode"))
		and (payload.get("Amount") or payload.get("amount") or payload.get("debitAmount"))
	)
