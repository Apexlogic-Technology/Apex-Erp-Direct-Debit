"""
apex_erp_direct_debit/install.py

Runs on:
  bench install-app apex_erp_direct_debit   → after_install
  bench migrate                              → after_migrate

Creates prerequisite ERPNext master data that the app depends on:
  - Mode of Payment: "Mobile Money - Direct Debit"
"""

import frappe


def after_install():
	"""Called once after `bench install-app`."""
	_ensure_mode_of_payment()
	frappe.db.commit()
	frappe.logger("apex_dd").info("[Install] Apex ERP Direct Debit install complete.")


def after_migrate():
	"""Called on every `bench migrate` — idempotent."""
	_ensure_mode_of_payment()
	frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────

def _ensure_mode_of_payment():
	"""Create 'Mobile Money - Direct Debit' Mode of Payment if it doesn't exist."""
	mop_name = "Mobile Money - Direct Debit"
	if frappe.db.exists("Mode of Payment", mop_name):
		return

	try:
		mop = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": mop_name,
			"type": "Bank",
			"enabled": 1,
		})
		mop.insert(ignore_permissions=True)
		frappe.logger("apex_dd").info(f"[Install] Created Mode of Payment: {mop_name}")
	except Exception:
		frappe.log_error(
			title="DD Install: Could not create Mode of Payment",
			message=frappe.get_traceback(),
		)
