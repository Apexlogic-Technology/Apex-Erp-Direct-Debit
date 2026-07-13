"""
Scheduler tasks for Apex ERP Direct Debit.
All functions are referenced in hooks.py scheduler_events.
"""

import frappe
from frappe.utils import add_to_date, flt, now_datetime, today, getdate


# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode — Poll pending mandates (runs every ~5 min via "all" queue)
# ─────────────────────────────────────────────────────────────────────────────

def poll_pending_mandates():
	"""
	Check Hubtel for mandates that have been Pending for more than 4 minutes.
	Hubtel sends a "PreapprovalStatus = FAILED" callback after 6 minutes of
	no response, but we poll proactively to catch edge cases.
	"""
	from apex_erp_direct_debit.services.provider_factory import get_provider

	cutoff = add_to_date(now_datetime(), minutes=-4)

	pending_mandates = frappe.get_all(
		"DD Mandate",
		filters={
			"mandate_status": "Pending",
			"source": "Hubtel",
			"modified": ["<", cutoff],
		},
		fields=["name", "company", "client_reference_id"],
		limit=50,
	)

	for row in pending_mandates:
		try:
			mandate = frappe.get_doc("DD Mandate", row.name)
			provider = get_provider(row.company)
			response = provider.check_mandate_status(mandate)

			preapproval_status = (
				response.get("data", {}).get("preapprovalStatus", "")
				or response.get("preapprovalStatus", "")
			).upper()

			if preapproval_status == "APPROVED":
				mandate.mark_approved(hubtel_data=response)
				_activate_debts_for_mandate(row.name)
			elif preapproval_status in ("FAILED", "EXPIRED"):
				mandate.mark_failed(reason=f"Polled status: {preapproval_status}", hubtel_data=response)

		except Exception:
			frappe.log_error(
				title=f"DD: poll_pending_mandates — error on {row.name}",
				message=frappe.get_traceback(),
			)


# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode — Poll pending transactions (runs every ~5 min)
# ─────────────────────────────────────────────────────────────────────────────

def poll_pending_transactions():
	"""
	Check Hubtel transaction status for debits that have been Pending for > 5 min.
	Catches cases where the debit callback was not received.
	"""
	from apex_erp_direct_debit.services.provider_factory import get_provider

	cutoff = add_to_date(now_datetime(), minutes=-5)

	pending_txns = frappe.get_all(
		"DD Transaction",
		filters={
			"status": "Pending",
			"initiated_at": ["<", cutoff],
		},
		fields=["name", "company", "client_reference_id", "debt", "installment_row"],
		limit=50,
	)

	for row in pending_txns:
		try:
			txn = frappe.get_doc("DD Transaction", row.name)
			provider = get_provider(row.company)
			response = provider.check_transaction_status(txn)

			code = (response.get("responseCode") or response.get("ResponseCode") or "").strip()

			if code == "0000":
				txn.mark_success(response)
				_mark_installment_paid(txn)
				_create_payment_entry_if_enabled(txn)
				_update_debt_totals(txn.debt)

			elif code in ("100", "101", "131"):
				reason = response.get("message") or f"Status poll: code {code}"
				txn.mark_failed(reason=reason, gateway_data=response)
				_mark_installment_failed(txn, reason)

			elif code == "111":
				txn.mark_inconclusive(response)

		except Exception:
			frappe.log_error(
				title=f"DD: poll_pending_transactions — error on {row.name}",
				message=frappe.get_traceback(),
			)


# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode — Process due installments (runs every minute)
# ─────────────────────────────────────────────────────────────────────────────

def process_due_installments():
	"""
	Find all Pending installments whose due_date <= today and trigger debits.
	Only processes Direct mode companies.
	A Frappe cache lock prevents parallel cron runs from double-debiting.
	"""
	from apex_erp_direct_debit.services.provider_factory import get_provider

	# ── Deduplication guard ──────────────────────────────────────────────────
	# If a previous run is still executing (cache key exists), skip this run
	lock_key = "dd_process_due_installments_lock"
	if frappe.cache().get_value(lock_key):
		frappe.logger("apex_dd").info("[Scheduler] process_due_installments: previous run still active, skipping.")
		return
	# Set lock with 5-minute TTL — auto-expires even if the task crashes
	frappe.cache().set_value(lock_key, 1, expires_in_sec=300)

	try:
		_run_due_installments()
	finally:
		frappe.cache().delete_value(lock_key)


def _run_due_installments():
	"""Inner function — called by process_due_installments after acquiring the lock."""
	from apex_erp_direct_debit.services.provider_factory import get_provider

	today_date = today()

	# Get active debts with pending installments due today or earlier
	due_rows = frappe.db.sql("""
		SELECT
			inst.name AS inst_name,
			inst.installment_amount,
			inst.due_date,
			debt.name AS debt_name,
			debt.company,
			debt.mandate,
			debt.customer
		FROM
			`tabDD Installment Schedule` inst
			INNER JOIN `tabDD Debt` debt ON debt.name = inst.parent
		WHERE
			inst.status = 'Pending'
			AND inst.due_date <= %(today)s
			AND debt.debt_status = 'Active'
		ORDER BY inst.due_date ASC
		LIMIT 100
	""", {"today": today_date}, as_dict=True)

	for row in due_rows:
		# Only process Direct - Hubtel companies
		mode = frappe.db.get_value("DD Settings", {"company": row.company}, "integration_mode")
		if mode != "Direct - Hubtel":
			continue

		# Check mandate is still approved
		mandate_status = frappe.db.get_value("DD Mandate", row.mandate, "mandate_status")
		if mandate_status != "Approved":
			frappe.logger("apex_dd").warning(
				f"[Scheduler] Skipping installment {row.inst_name} — mandate not Approved"
			)
			continue

		try:
			# Mark as Processing to prevent double-triggering
			frappe.db.set_value("DD Installment Schedule", row.inst_name, "status", "Processing")

			txn = frappe.get_doc({
				"doctype":        "DD Transaction",
				"debt":           row.debt_name,
				"mandate":        row.mandate,
				"company":        row.company,
				"amount":         row.installment_amount,
				"status":         "Pending",
				"installment_row": row.inst_name,
				"initiated_at":   now_datetime(),
			})
			txn.insert(ignore_permissions=True)

			frappe.db.set_value(
				"DD Installment Schedule", row.inst_name, "dd_transaction", txn.name
			)

			provider = get_provider(row.company)
			response = provider.trigger_debit(txn)

			rc = response.get("responseCode") or response.get("ResponseCode", "")
			if rc not in ("0001", "03", "01"):
				# Trigger rejected — mark failed immediately
				txn.mark_failed(
					reason=response.get("message") or f"Rejected: {rc}",
					gateway_data=response,
				)
				frappe.db.set_value("DD Installment Schedule", row.inst_name, "status", "Failed")
				current_retries = frappe.db.get_value(
					"DD Installment Schedule", row.inst_name, "retry_count"
				) or 0
				frappe.db.set_value(
					"DD Installment Schedule", row.inst_name, "retry_count", current_retries + 1
				)

			frappe.logger("apex_dd").info(
				f"[Scheduler] Triggered debit for installment {row.inst_name} | "
				f"txn={txn.name} | rc={rc}"
			)

		except Exception:
			frappe.log_error(
				title=f"DD: process_due_installments — error on installment {row.inst_name}",
				message=frappe.get_traceback(),
			)
			frappe.db.set_value("DD Installment Schedule", row.inst_name, "status", "Pending")


# ─────────────────────────────────────────────────────────────────────────────
# Bridge Mode — Sync transactions from KolectPay/SMCollect (every 15 min)
# ─────────────────────────────────────────────────────────────────────────────

def sync_from_bridge():
	"""
	Pull new transactions from KolectPay/SMCollect and create corresponding
	DD Transaction records + Payment Entries in ERPNext.
	"""
	from apex_erp_direct_debit.services.provider_factory import get_provider

	bridge_settings = frappe.get_all(
		"DD Settings",
		filters={
			"is_enabled": 1,
			"integration_mode": ["in", ["Bridge - KolectPay Business", "Bridge - SMCollect"]],
		},
		fields=["name", "company"],
	)

	for s in bridge_settings:
		try:
			# Pull transactions since last sync (stored in a cache key)
			cache_key = f"dd_bridge_last_sync_{s.company}"
			last_sync = frappe.cache().get_value(cache_key)

			provider = get_provider(s.company)
			transactions = provider.sync_transactions(s.company, since_datetime=last_sync)

			for txn_data in transactions:
				_upsert_bridge_transaction(txn_data, s.company)

			frappe.cache().set_value(cache_key, str(now_datetime()))

		except Exception:
			frappe.log_error(
				title=f"DD: sync_from_bridge — error for company {s.company}",
				message=frappe.get_traceback(),
			)


# ─────────────────────────────────────────────────────────────────────────────
# Direct Mode — SMS Reminders (daily 08:00)
# ─────────────────────────────────────────────────────────────────────────────

def send_debit_reminders():
	"""
	Send SMS and/or WhatsApp reminders to customers whose next installment is due
	in `sms_days_before` days. Only for Direct - Hubtel companies.
	"""
	direct_settings = frappe.get_all(
		"DD Settings",
		filters={
			"is_enabled": 1,
			"integration_mode": "Direct - Hubtel",
		},
		fields=[
			"company", "sms_days_before", "send_sms_reminders", 
			"sms_provider", "sms_api_key", "send_whatsapp_reminders",
			"whatsapp_api_url", "whatsapp_api_token", "whatsapp_sender_phone"
		],
	)

	for s in direct_settings:
		if not s.send_sms_reminders and not s.send_whatsapp_reminders:
			continue

		days_before = int(s.sms_days_before or 1)
		target_date = frappe.utils.add_days(today(), days_before)

		due_debts = frappe.db.sql("""
			SELECT
				debt.name, debt.customer, debt.company,
				mandate.customer_name,
				mandate.mobile_number_formatted,
				inst.installment_amount, inst.due_date
			FROM `tabDD Debt` debt
			INNER JOIN `tabDD Mandate` mandate ON mandate.name = debt.mandate
			INNER JOIN `tabDD Installment Schedule` inst ON inst.parent = debt.name
			WHERE
				debt.debt_status = 'Active'
				AND debt.company = %(company)s
				AND inst.status = 'Pending'
				AND inst.due_date = %(target_date)s
		""", {"company": s.company, "target_date": str(target_date)}, as_dict=True)

		for row in due_debts:
			if s.send_sms_reminders:
				try:
					_send_sms_reminder(row, s)
				except Exception:
					frappe.log_error(
						title=f"DD: SMS reminder failed for {row.customer}",
						message=frappe.get_traceback(),
					)
			if s.send_whatsapp_reminders:
				try:
					_send_whatsapp_reminder(row, s)
				except Exception:
					frappe.log_error(
						title=f"DD: WhatsApp reminder failed for {row.customer}",
						message=frappe.get_traceback(),
					)


# ─────────────────────────────────────────────────────────────────────────────
# Daily Reconciliation (01:00)
# ─────────────────────────────────────────────────────────────────────────────

def generate_reconciliation():
	"""Generate a daily DD Reconciliation Report for all enabled companies."""
	from frappe.utils import add_days

	yesterday = str(add_days(today(), -1))

	all_settings = frappe.get_all(
		"DD Settings",
		filters={"is_enabled": 1},
		fields=["company"],
	)

	for s in all_settings:
		try:
			_build_reconciliation_report(s.company, yesterday, yesterday)
		except Exception:
			frappe.log_error(
				title=f"DD: generate_reconciliation failed for {s.company}",
				message=frappe.get_traceback(),
			)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers (shared between tasks and webhook handler)
# ─────────────────────────────────────────────────────────────────────────────

def _activate_debts_for_mandate(mandate_name: str):
	debts = frappe.get_all(
		"DD Debt",
		filters={"mandate": mandate_name, "debt_status": "Draft"},
		pluck="name",
	)
	for d in debts:
		frappe.db.set_value("DD Debt", d, "debt_status", "Active")


def _mark_installment_paid(txn):
	if txn.installment_row:
		frappe.db.set_value("DD Installment Schedule", txn.installment_row, {
			"status": "Paid",
			"paid_on": now_datetime(),
			"dd_transaction": txn.name,
		})


def _mark_installment_failed(txn, reason: str):
	if txn.installment_row:
		frappe.db.set_value("DD Installment Schedule", txn.installment_row, {
			"status": "Failed",
			"failure_reason": (reason or "")[:140],
		})
		current = frappe.db.get_value("DD Installment Schedule", txn.installment_row, "retry_count") or 0
		frappe.db.set_value("DD Installment Schedule", txn.installment_row, "retry_count", current + 1)


def _create_payment_entry_if_enabled(txn):
	"""Lightweight wrapper — delegates to webhook.py's implementation."""
	from apex_erp_direct_debit.api.webhook import _create_payment_entry_if_enabled as _create_pe
	_create_pe(txn, flt(txn.amount))


def _update_debt_totals(debt_name: str):
	debt = frappe.get_doc("DD Debt", debt_name)
	debt.recalculate_totals()


def _upsert_bridge_transaction(txn_data: dict, company: str):
	"""Create a DD Transaction in ERPNext from bridge sync data if not already present."""
	gateway_id = txn_data.get("id") or txn_data.get("transaction_id") or ""
	if not gateway_id:
		return

	existing = frappe.db.get_value("DD Transaction", {"gateway_txn_id": gateway_id}, "name")
	if existing:
		return  # Already synced

	# Try to match to a DD Debt by bridge reference
	bridge_debt_id = str(txn_data.get("debt_id") or txn_data.get("debtId") or "")
	debt_name = frappe.db.get_value("DD Debt", {"bridge_debt_id": bridge_debt_id}, "name")

	txn = frappe.get_doc({
		"doctype":        "DD Transaction",
		"debt":           debt_name or "",
		"company":        company,
		"amount":         flt(txn_data.get("amount") or 0),
		"charge":         flt(txn_data.get("charge") or 0),
		"status":         "Success" if txn_data.get("status") == "success" else "Failed",
		"gateway_txn_id": gateway_id,
		"channel":        txn_data.get("channel", ""),
		"initiated_at":   txn_data.get("created_at") or now_datetime(),
		"raw_callback":   frappe.as_json(txn_data),
	})
	txn.insert(ignore_permissions=True)

	if txn.status == "Success" and debt_name:
		_update_debt_totals(debt_name)


# _send_sms_reminder and _send_whatsapp_reminder defined near bottom (with audit logging)


def _build_reconciliation_report(company: str, period_start: str, period_end: str):
	txns = frappe.get_all(
		"DD Transaction",
		filters={
			"company": company,
			"initiated_at": ["between", [period_start + " 00:00:00", period_end + " 23:59:59"]],
		},
		fields=["status", "amount", "charge"],
	)

	report = frappe.get_doc({
		"doctype":                "DD Reconciliation Report",
		"company":                company,
		"period_start":           period_start,
		"period_end":             period_end,
		"total_debits_attempted": len(txns),
		"total_debits_success":   sum(1 for t in txns if t.status == "Success"),
		"total_debits_failed":    sum(1 for t in txns if t.status == "Failed"),
		"total_amount_collected": sum(flt(t.amount) for t in txns if t.status == "Success"),
		"total_gateway_charges":  sum(flt(t.charge) for t in txns if t.status == "Success"),
		"unmatched_transactions": sum(1 for t in txns if t.status == "Inconclusive"),
		"status":                 "Submitted",
		"report_data":            frappe.as_json(txns),
	})
	report.insert(ignore_permissions=True)
	frappe.logger("apex_dd").info(
		f"[Scheduler] Reconciliation report {report.name} generated for {company} on {period_start}"
	)


# ─────────────────────────────────────────────────────────────────────────────
# Notification helpers (SMS, WhatsApp — with audit logging)
# ─────────────────────────────────────────────────────────────────────────────

def _notify_log(company, customer, phone, channel, message_type, message, status, error=None):
	"""Create a DD Notification Log record for audit trail."""
	try:
		from apex_erp_direct_debit.direct_debit.doctype.dd_notification_log.dd_notification_log import DDNotificationLog
		DDNotificationLog.log_notification(
			company=company,
			customer=customer,
			contact_number=phone,
			channel=channel,
			message_type=message_type,
			message=message,
			status=status,
			error_details=str(error) if error else None,
		)
	except Exception:
		frappe.logger("apex_dd").warning(f"[Notification Log] Failed to write log for {customer}")


def _send_sms_reminder(row: dict, settings):
	"""Send a pre-debit SMS reminder and log the result."""
	phone = row.mobile_number_formatted
	amount = frappe.utils.fmt_money(row.installment_amount, currency="GHS")
	due = frappe.utils.formatdate(row.due_date)
	message = (
		f"Dear Customer, GHS {amount} will be debited from your mobile wallet "
		f"on {due} for your installment payment. Ensure sufficient funds."
	)
	status = "Failed"
	err = None

	if settings.sms_provider == "Hubtel SMS" and settings.sms_api_key:
		import requests
		try:
			requests.post(
				"https://smsc.hubtel.com/v1/messages/send",
				params={
					"clientid":     settings.sms_api_key,
					"clientsecret": "",
					"from":         "DD-Alert",
					"to":           phone,
					"content":      message,
				},
				timeout=10,
			)
			status = "Success"
			frappe.logger("apex_dd").info(f"[Scheduler] SMS reminder sent to {phone}: {message}")
		except Exception as e:
			err = e
			frappe.log_error(title=f"DD: SMS reminder send failed to {phone}", message=str(e))
	else:
		# No credentials — simulate
		frappe.logger("apex_dd").info(f"[SMS Simulation] To: {phone} | Msg: {message}")
		status = "Success"

	_notify_log(settings.company, row.customer, phone, "SMS", "Pre-Debit Reminder", message, status, err)


def _send_whatsapp_reminder(row: dict, settings):
	"""Send a WhatsApp nudge reminder and log the result."""
	phone = row.mobile_number_formatted
	if not phone:
		return
	if not phone.startswith("+") and not phone.startswith("233"):
		phone = "233" + phone.lstrip("0")
	elif phone.startswith("+"):
		phone = phone.replace("+", "")

	amount = frappe.utils.fmt_money(row.installment_amount, currency="GHS")
	customer_first_name = (getattr(row, "customer_name", None) or "").split(" ")[0] or "Customer"

	message = (
		f"👋 Hello {customer_first_name},\n\n"
		f"This is a friendly reminder that your scheduled payment of *GH₵ {amount}* for *{settings.company}* "
		f"is due on *{frappe.utils.formatdate(row.due_date)}*.\n\n"
		f"Please ensure your Mobile Money wallet has sufficient funds. Thank you!"
	)

	url = settings.whatsapp_api_url or ""
	token = settings.get_password("whatsapp_api_token") if settings.whatsapp_api_token else None
	sender = settings.whatsapp_sender_phone
	status = "Failed"
	err = None

	if not token:
		frappe.logger("apex_dd").info(f"[WhatsApp Simulation] To: {phone} | Msg: {message}")
		_notify_log(settings.company, row.customer, phone, "WhatsApp", "Pre-Debit Reminder", message, "Success")
		return

	import requests
	try:
		headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
		payload = {
			"to": phone, "from": sender,
			"type": "text", "text": {"body": message}
		}
		resp = requests.post(url, json=payload, headers=headers, timeout=10)
		resp.raise_for_status()
		status = "Success"
		frappe.logger("apex_dd").info(f"[Scheduler] WhatsApp reminder sent to {phone}")
	except Exception as e:
		err = e
		frappe.log_error(title=f"DD: WhatsApp send failed to {phone}", message=str(e))

	_notify_log(settings.company, row.customer, phone, "WhatsApp", "Pre-Debit Reminder", message, status, err)


def _send_failure_alert(customer: str, phone: str, company: str,
						amount: float, retry_in_days: int, channel: str = "SMS"):
	"""Send a failed debit nudge to the customer via SMS or WhatsApp."""
	if not phone:
		return
	formatted_phone = phone
	if not phone.startswith("233") and not phone.startswith("+"):
		formatted_phone = "233" + phone.lstrip("0")
	elif phone.startswith("+"):
		formatted_phone = phone.replace("+", "")

	amount_fmt = frappe.utils.fmt_money(amount, currency="GHS")
	message = (
		f"⚠️ Hello,\n\nWe noticed your auto-debit of *GH₵ {amount_fmt}* "
		f"for *{company}* was unsuccessful, likely due to insufficient funds.\n\n"
		f"We will retry the charge in {retry_in_days} day(s). "
		f"Please ensure your wallet has sufficient funds to avoid service disruption. Thank you."
	)

	# Use get_doc to correctly decrypt Password fields
	settings_list = frappe.get_all("DD Settings", filters={"company": company, "is_enabled": 1}, pluck="name", limit=1)
	if not settings_list:
		frappe.logger("apex_dd").warning(f"[Failure Alert] No DD Settings found for {company}")
		return
	settings_doc = frappe.get_doc("DD Settings", settings_list[0])
	status = "Failed"
	err = None

	if channel == "WhatsApp":
		token = settings_doc.get_password("whatsapp_api_token") if settings_doc.whatsapp_api_token else None
		url = settings_doc.whatsapp_api_url or ""
		sender = settings_doc.whatsapp_sender_phone or ""
		if not token or not url:
			frappe.logger("apex_dd").info(f"[WhatsApp Failure Simulation] To: {formatted_phone} | {message}")
			_notify_log(company, customer, formatted_phone, "WhatsApp", "Failure Alert", message, "Success")
			return
		import requests
		try:
			headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
			requests.post(url, json={
				"to": formatted_phone, "from": sender,
				"type": "text", "text": {"body": message}
			}, headers=headers, timeout=10).raise_for_status()
			status = "Success"
		except Exception as e:
			err = e
			frappe.log_error(title=f"DD: WA failure alert failed to {formatted_phone}", message=str(e))
	else:
		# SMS via Hubtel
		api_key = settings_doc.get_password("sms_api_key") if settings_doc.sms_api_key else ""
		sms_msg = (
			f"Your DD of GHS {frappe.utils.fmt_money(amount, currency='GHS')} "
			f"for {company} failed. We retry in {retry_in_days} day(s). Ensure funds."
		)
		if api_key:
			import requests
			try:
				requests.post(
					"https://smsc.hubtel.com/v1/messages/send",
					params={"clientid": api_key, "clientsecret": "", "from": "DD-Alert",
							"to": formatted_phone, "content": sms_msg},
					timeout=10,
				)
				status = "Success"
			except Exception as e:
				err = e
				frappe.log_error(title=f"DD: SMS failure alert failed to {formatted_phone}", message=str(e))
		else:
			frappe.logger("apex_dd").info(f"[SMS Failure Simulation] To: {formatted_phone} | {sms_msg}")
			status = "Success"
		message = sms_msg

	_notify_log(company, customer, formatted_phone, channel, "Failure Alert", message, status, err)


# ─────────────────────────────────────────────────────────────────────────────
# Auto-retry failed installments  (runs every 30 min via "daily_long")
# ─────────────────────────────────────────────────────────────────────────────

def retry_failed_installments():
	"""
	Re-attempt debits for failed installments where:
	  - retry_count < max_retry_attempts (from DD Settings)
	  - last failure was >= retry_interval_days ago
	  - debt is Active and mandate is Approved (Direct - Hubtel only)
	"""
	from apex_erp_direct_debit.services.provider_factory import get_provider
	from frappe.utils import add_days

	direct_settings = frappe.get_all(
		"DD Settings",
		filters={"is_enabled": 1, "integration_mode": "Direct - Hubtel"},
		fields=["company", "max_retry_attempts", "retry_interval_days", "send_failure_alerts",
				"send_sms_reminders", "send_whatsapp_reminders"],
	)

	for s in direct_settings:
		max_retries = int(s.max_retry_attempts or 3)
		retry_days = int(s.retry_interval_days or 1)
		retry_cutoff = str(add_days(today(), -retry_days))

		failed_rows = frappe.db.sql("""
			SELECT
				inst.name AS inst_name, inst.retry_count,
				inst.installment_amount, inst.due_date,
				debt.name AS debt_name, debt.company, debt.customer,
				debt.mandate, debt.debt_status,
				mandate.mobile_number_formatted, mandate.mandate_status
			FROM `tabDD Installment Schedule` inst
			INNER JOIN `tabDD Debt` debt ON debt.name = inst.parent
			LEFT JOIN `tabDD Mandate` mandate ON mandate.name = debt.mandate
			WHERE
				inst.status = 'Failed'
				AND debt.debt_status = 'Active'
				AND debt.company = %(company)s
				AND COALESCE(inst.retry_count, 0) < %(max_retries)s
				AND (inst.modified <= %(cutoff)s OR inst.modified IS NULL)
		""", {
			"company": s.company,
			"max_retries": max_retries,
			"cutoff": retry_cutoff + " 23:59:59",
		}, as_dict=True)

		for row in failed_rows:
			if row.mandate_status != "Approved":
				continue

			try:
				# Mark as Processing to prevent double-triggering
				frappe.db.set_value("DD Installment Schedule", row.inst_name, "status", "Processing")

				txn = frappe.get_doc({
					"doctype":         "DD Transaction",
					"debt":            row.debt_name,
					"mandate":         row.mandate,
					"company":         row.company,
					"amount":          row.installment_amount,
					"status":          "Pending",
					"installment_row": row.inst_name,
					"initiated_at":    now_datetime(),
				})
				txn.insert(ignore_permissions=True)
				frappe.db.set_value("DD Installment Schedule", row.inst_name, "dd_transaction", txn.name)

				provider = get_provider(row.company)
				response = provider.trigger_debit(txn)

				rc = response.get("responseCode") or response.get("ResponseCode", "")
				if rc not in ("0001", "03", "01"):
					txn.mark_failed(
						reason=response.get("message") or f"Retry rejected: {rc}",
						gateway_data=response,
					)
					current = frappe.db.get_value("DD Installment Schedule", row.inst_name, "retry_count") or 0
					frappe.db.set_value("DD Installment Schedule", row.inst_name, {
						"status": "Failed",
						"retry_count": current + 1,
					})
					frappe.logger("apex_dd").warning(
						f"[Retry] Installment {row.inst_name} retry rejected: {rc}"
					)
				else:
					frappe.logger("apex_dd").info(
						f"[Retry] Installment {row.inst_name} retry triggered | txn={txn.name}"
					)

			except Exception:
				frappe.log_error(
					title=f"DD: retry_failed_installments error on {row.inst_name}",
					message=frappe.get_traceback(),
				)
				frappe.db.set_value("DD Installment Schedule", row.inst_name, "status", "Failed")


# ─────────────────────────────────────────────────────────────────────────────
# Mandate Auto-Expiry  (runs daily at 02:00)
# ─────────────────────────────────────────────────────────────────────────────

def expire_old_mandates():
	"""
	Find Approved mandates whose expires_at has passed and mark them Expired.
	Also pauses any Active DD Debts linked to expired mandates to prevent
	further debit attempts against an expired consent.
	"""
	expired = frappe.db.sql("""
		SELECT name, company
		FROM `tabDD Mandate`
		WHERE
			mandate_status = 'Approved'
			AND expires_at IS NOT NULL
			AND expires_at <= %(now)s
	""", {"now": str(now_datetime())}, as_dict=True)

	for row in expired:
		try:
			frappe.db.set_value("DD Mandate", row.name, "mandate_status", "Expired")
			frappe.logger("apex_dd").info(f"[Expire] Mandate {row.name} auto-expired.")

			# Pause any active debts attached to this mandate
			active_debts = frappe.get_all(
				"DD Debt",
				filters={"mandate": row.name, "debt_status": "Active"},
				pluck="name",
			)
			for debt_name in active_debts:
				frappe.db.set_value("DD Debt", debt_name, "debt_status", "Paused")
				frappe.logger("apex_dd").info(
					f"[Expire] Paused DD Debt {debt_name} — mandate {row.name} expired."
				)
		except Exception:
			frappe.log_error(
				title=f"DD: expire_old_mandates error on {row.name}",
				message=frappe.get_traceback(),
			)
