"""
Whitelisted mandate management API endpoints.
Called from the Customer and DD Mandate form buttons (JS).

Each function is decorated with @frappe.whitelist() so ERPNext's
`frappe.call()` can invoke them from the browser.
"""

import frappe
from frappe import _

from apex_erp_direct_debit.services.provider_factory import get_provider


@frappe.whitelist()
def initiate_mandate(mandate_name: str) -> dict:
	"""
	Initiate a Hubtel Pre-Approval or push a mandate to the bridge.
	Called from Customer form → 'Initiate Mandate' button.
	"""
	mandate = frappe.get_doc("DD Mandate", mandate_name)
	_require_permission(mandate)

	if mandate.mandate_status not in ("Draft", "Failed", "Expired"):
		frappe.throw(
			_(f"Cannot initiate mandate with status '{mandate.mandate_status}'. "
			  "Reset to Draft first."),
			title=_("Invalid Mandate Status"),
		)

	provider = get_provider(mandate.company)
	response = provider.initiate_mandate(mandate)

	rc = response.get("responseCode", "")
	if rc == "2000":
		return {
			"success": True,
			"verification_type": response.get("data", {}).get("verificationType", "USSD"),
			"otp_prefix": response.get("data", {}).get("otpPrefix", ""),
			"message": _("Mandate initiated. Customer will receive USSD prompt or OTP."),
		}
	else:
		return {
			"success": False,
			"message": response.get("message") or response.get("error") or _("Mandate initiation failed."),
			"raw": response,
		}


@frappe.whitelist()
def verify_otp(mandate_name: str, otp_code: str) -> dict:
	"""
	Submit the OTP code entered by the admin (relayed from customer).
	Called from the OTP dialog on the Customer/Mandate form.
	"""
	mandate = frappe.get_doc("DD Mandate", mandate_name)
	_require_permission(mandate)

	if not otp_code or not otp_code.strip():
		frappe.throw(_("OTP code is required."))

	if mandate.verification_type != "OTP":
		frappe.throw(_(f"Mandate {mandate_name} uses USSD verification, not OTP."))

	provider = get_provider(mandate.company)
	response = provider.verify_otp(mandate, otp_code.strip())

	rc = response.get("responseCode", "")
	if rc == "2000":
		return {"success": True, "message": _("OTP verified. Awaiting Hubtel approval callback.")}
	else:
		return {
			"success": False,
			"message": response.get("message") or response.get("error") or _("OTP verification failed."),
			"raw": response,
		}


@frappe.whitelist()
def cancel_mandate(mandate_name: str) -> dict:
	"""Cancel an active mandate."""
	mandate = frappe.get_doc("DD Mandate", mandate_name)
	_require_permission(mandate)

	if mandate.mandate_status not in ("Pending", "Approved"):
		frappe.throw(
			_(f"Only Pending or Approved mandates can be cancelled. "
			  f"Current status: {mandate.mandate_status}"),
		)

	provider = get_provider(mandate.company)
	response = provider.cancel_mandate(mandate)

	rc = response.get("responseCode", "")
	if rc == "2000":
		return {"success": True, "message": _("Mandate cancelled successfully.")}
	else:
		return {
			"success": False,
			"message": response.get("message") or response.get("error") or _("Cancellation failed."),
		}


@frappe.whitelist()
def reactivate_mandate(mandate_name: str) -> dict:
	"""Reactivate a cancelled mandate."""
	mandate = frappe.get_doc("DD Mandate", mandate_name)
	_require_permission(mandate)

	if mandate.mandate_status != "Cancelled":
		frappe.throw(_(f"Only Cancelled mandates can be reactivated."))

	provider = get_provider(mandate.company)
	response = provider.reactivate_mandate(mandate)

	rc = response.get("responseCode", "")
	if rc == "2000":
		return {
			"success": True,
			"verification_type": response.get("data", {}).get("verificationType", "USSD"),
			"otp_prefix": response.get("data", {}).get("otpPrefix", ""),
			"message": _("Mandate reactivation initiated."),
		}
	else:
		return {"success": False, "message": response.get("message") or _("Reactivation failed.")}


@frappe.whitelist()
def check_mandate_status(mandate_name: str) -> dict:
	"""Poll the current mandate status from the gateway."""
	mandate = frappe.get_doc("DD Mandate", mandate_name)
	provider = get_provider(mandate.company)
	response = provider.check_mandate_status(mandate)
	return {"success": True, "response": response}


@frappe.whitelist()
def trigger_debit_now(debt_name: str, installment_idx: int = None, custom_amount: float = None) -> dict:
	"""
	Manually trigger a debit for the next pending installment (or a specific one).
	Optionally pass `custom_amount` to debit a partial amount instead of the full installment.
	If the paid amount is less than the installment amount, the remainder is split into a new
	Pending row in the installment schedule.
	Called from the DD Debt form → 'Trigger Debit Now' button.
	"""
	from frappe.utils import flt, now_datetime, add_months

	debt = frappe.get_doc("DD Debt", debt_name)

	if debt.debt_status != "Active":
		frappe.throw(_(f"DD Debt must be Active to trigger a debit. Status: {debt.debt_status}"))

	if not debt.mandate:
		frappe.throw(_("No mandate linked to this DD Debt."))

	mandate = frappe.get_doc("DD Mandate", debt.mandate)
	if mandate.mandate_status != "Approved":
		frappe.throw(_(f"Mandate {debt.mandate} is not Approved. Status: {mandate.mandate_status}"))

	# Find the target installment row
	target_row = None
	if installment_idx is not None:
		rows = debt.installment_schedule
		if 0 <= int(installment_idx) < len(rows):
			target_row = rows[int(installment_idx)]
	else:
		# First pending installment
		target_row = next(
			(r for r in debt.installment_schedule if r.status == "Pending"),
			None,
		)

	if not target_row:
		frappe.throw(_("No pending installment found to debit."))

	debit_amount = flt(custom_amount) if custom_amount else flt(target_row.installment_amount)
	full_amount = flt(target_row.installment_amount)

	if debit_amount <= 0:
		frappe.throw(_("Debit amount must be greater than zero."))
	if debit_amount > full_amount:
		frappe.throw(_(f"Custom amount ({debit_amount}) cannot exceed installment amount ({full_amount})."))

	# Create a DD Transaction record
	txn = frappe.get_doc({
		"doctype": "DD Transaction",
		"debt":    debt.name,
		"mandate": debt.mandate,
		"company": debt.company,
		"amount":  debit_amount,
		"status":  "Pending",
		"installment_row": target_row.name,
		"initiated_at": now_datetime(),
	})
	txn.insert(ignore_permissions=True)

	# Mark installment as Processing
	frappe.db.set_value(
		"DD Installment Schedule",
		target_row.name,
		{"status": "Processing", "dd_transaction": txn.name},
	)

	provider = get_provider(debt.company)
	response = provider.trigger_debit(txn)

	rc = response.get("responseCode") or response.get("ResponseCode", "")
	if rc in ("0001", "03", "01"):  # accepted / processing
		# If partial debit: split remainder into a new schedule row
		if debit_amount < full_amount:
			remainder = flt(full_amount - debit_amount, 2)
			# Compute a new due date (next month from current row)
			new_due = add_months(target_row.due_date, 1) if target_row.due_date else frappe.utils.today()
			debt_doc = frappe.get_doc("DD Debt", debt_name)
			debt_doc.append("installment_schedule", {
				"due_date":            new_due,
				"installment_amount":  remainder,
				"status":              "Pending",
				"remarks":             f"Remainder from partial debit on {frappe.utils.today()} (Txn: {txn.name})",
			})
			debt_doc.save(ignore_permissions=True)
			frappe.logger("apex_dd").info(
				f"[Mandate API] Partial debit: GHS {debit_amount}/{full_amount} | "
				f"Remainder GHS {remainder} → new schedule row added | debt={debt_name}"
			)

		return {
			"success": True,
			"transaction": txn.name,
			"is_partial": debit_amount < full_amount,
			"debit_amount": debit_amount,
			"message": _(
				f"Debit of GHS {debit_amount} triggered. Awaiting callback from gateway."
				+ (f" Remainder GHS {flt(full_amount - debit_amount, 2)} added as new installment." if debit_amount < full_amount else "")
			),
		}
	else:
		txn.mark_failed(
			reason=response.get("message") or response.get("error") or "Trigger rejected",
			gateway_data=response,
		)
		frappe.db.set_value("DD Installment Schedule", target_row.name, "status", "Failed")
		return {
			"success": False,
			"transaction": txn.name,
			"message": response.get("message") or _("Debit trigger rejected by gateway."),
		}


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _require_permission(doc):
	"""Ensure current user has write access to this document."""
	if not frappe.has_permission(doc.doctype, "write", doc):
		frappe.throw(_("You do not have permission to perform this action."), frappe.PermissionError)
