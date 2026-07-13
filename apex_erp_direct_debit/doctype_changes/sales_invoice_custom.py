"""
Sales Invoice doctype hooks.

on_submit: Notify the admin about creating a DD Debt (non-blocking).
on_cancel:  Pause any active DD Debt linked to this invoice.
"""

import frappe
from frappe import _


def on_submit(doc, method=None):
	"""
	When a Sales Invoice is submitted, check if DD is configured and the customer
	has an approved mandate — if so, show a notification prompting the admin
	to create a DD Debt.

	This is non-blocking: if the admin ignores it, nothing happens automatically.
	The 'Create DD Debt' button on the form handles actual debt creation.
	"""
	company = doc.company
	if not frappe.db.exists("DD Settings", company):
		return

	settings = frappe.get_doc("DD Settings", company)
	if not settings.is_enabled or not settings.auto_create_debt_on_invoice:
		return

	# Check if customer has an approved mandate
	mandate = frappe.db.get_value(
		"DD Mandate",
		{"customer": doc.customer, "mandate_status": "Approved"},
		"name",
	)
	if not mandate:
		return

	frappe.msgprint(
		_(
			f"Customer <b>{doc.customer_name}</b> has an approved Direct Debit mandate. "
			f"<br>Click <b>'Create DD Debt'</b> on this invoice to set up installment collection."
		),
		title=_("Direct Debit Available"),
		indicator="blue",
	)


def on_cancel(doc, method=None):
	"""
	When a Sales Invoice is cancelled, pause any linked active DD Debt
	to prevent further collections against a cancelled invoice.
	"""
	debts = frappe.get_all(
		"DD Debt",
		filters={"sales_invoice": doc.name, "debt_status": "Active"},
		pluck="name",
	)
	for debt_name in debts:
		frappe.db.set_value("DD Debt", debt_name, "debt_status", "Paused")
		frappe.logger("apex_dd").info(
			f"[Invoice Cancel] Paused DD Debt {debt_name} because Invoice {doc.name} was cancelled."
		)


@frappe.whitelist()
def create_dd_debt(invoice_name: str, collection_type: str, frequency: str,
                   num_installments: int, start_date: str, description: str = "") -> dict:
	"""
	Whitelisted: create a DD Debt from a Sales Invoice.
	Called from the 'Create DD Debt' button on the Sales Invoice form.
	"""
	invoice = frappe.get_doc("Sales Invoice", invoice_name)

	if invoice.docstatus != 1:
		frappe.throw(_("Sales Invoice must be submitted before creating a DD Debt."))

	if frappe.db.exists("DD Debt", {"sales_invoice": invoice_name, "debt_status": ["in", ["Draft", "Active"]]}):
		frappe.throw(_("An active DD Debt already exists for this Sales Invoice."))

	# Find or prompt for mandate
	mandate_name = frappe.db.get_value(
		"DD Mandate",
		{"customer": invoice.customer, "mandate_status": "Approved"},
		"name",
	)

	debt = frappe.get_doc({
		"doctype":            "DD Debt",
		"company":            invoice.company,
		"customer":           invoice.customer,
		"sales_invoice":      invoice_name,
		"mandate":            mandate_name,
		"total_amount":       invoice.outstanding_amount,
		"collection_type":    collection_type,
		"installment_frequency": frequency if collection_type != "One-Time" else None,
		"number_of_installments": int(num_installments) if collection_type == "Installment" else None,
		"start_date":         start_date,
		"description":        description or f"Installment collection for Invoice {invoice_name}",
		"narration":          f"Direct debit - {invoice.customer_name} - {invoice_name}",
		"currency":           invoice.currency,
		"debt_status":        "Active" if mandate_name else "Draft",
	})
	debt.insert(ignore_permissions=True)

	return {
		"success": True,
		"debt":    debt.name,
		"message": _(f"DD Debt <b>{debt.name}</b> created with installment schedule."),
	}
