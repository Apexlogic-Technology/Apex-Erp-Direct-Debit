"""
Customer doctype hook.

after_save:  Auto-push customer to KolectPay/SMCollect as a Debtor when
             the dd_mobile_number custom field is populated (Bridge mode only).
"""

import frappe
from apex_erp_direct_debit.services.provider_factory import get_provider


def after_save(doc, method=None):
	"""
	Called after any Customer save. If:
	  - the customer has a dd_mobile_number set, and
	  - DD Settings for their company are in Bridge mode with auto_push enabled
	  - and no bridge_debtor_id has been set yet
	→ push them to KolectPay / SMCollect as a Debtor.
	"""
	mobile = getattr(doc, "dd_mobile_number", None)
	if not mobile:
		return

	company = _get_default_company()
	if not company:
		return

	try:
		settings = frappe.get_doc("DD Settings", company)
	except frappe.DoesNotExistError:
		return

	if not settings.is_enabled:
		return

	if settings.integration_mode not in ("Bridge - KolectPay Business", "Bridge - SMCollect"):
		return

	if not settings.auto_push_customer:
		return

	# Check if already pushed
	if getattr(doc, "dd_bridge_debtor_id", None):
		return

	try:
		provider = get_provider(company)
		response = provider.create_debtor(doc)
		debtor_id = (
			response.get("data", {}).get("id")
			or response.get("id")
			or response.get("debtor_id")
		)
		if debtor_id:
			frappe.db.set_value("Customer", doc.name, "dd_bridge_debtor_id", str(debtor_id))
			frappe.logger("apex_dd").info(
				f"[Customer Hook] Pushed {doc.name} → Bridge as debtor {debtor_id}"
			)
	except Exception:
		frappe.log_error(
			title=f"DD: Failed to push customer {doc.name} to bridge",
			message=frappe.get_traceback(),
		)


def _get_default_company() -> str | None:
	"""Return the default ERPNext company, or the first one with DD Settings."""
	default = frappe.db.get_default("company")
	if default:
		return default
	row = frappe.db.get_value("DD Settings", filters={"is_enabled": 1}, fieldname="company")
	return row or None
