"""
Provider Factory — returns the correct DirectDebitProviderBase implementation
based on the DD Settings `integration_mode` for a given Company.
"""

import frappe

from apex_erp_direct_debit.services.hubtel_service import HubtelService
from apex_erp_direct_debit.services.kolectpay_bridge import KolectPayBridge
from apex_erp_direct_debit.services.smcollect_bridge import SMCollectBridge


def get_provider(company: str):
	"""
	Retrieve DD Settings for the given company and return the appropriate
	service provider instance.

	Raises frappe.ValidationError if:
	  - No DD Settings exist for the company
	  - DD Settings are disabled
	  - integration_mode is not recognised
	"""
	settings = frappe.db.get_value(
		"DD Settings",
		{"company": company},
		["name", "is_enabled", "integration_mode"],
		as_dict=True,
	)

	if not settings:
		frappe.throw(
			f"No DD Settings found for company '{company}'. "
			"Please configure Direct Debit settings first.",
			title="DD Settings Missing",
		)

	if not settings.is_enabled:
		frappe.throw(
			f"Direct Debit is disabled for company '{company}'. "
			"Enable it in DD Settings.",
			title="DD Disabled",
		)

	# Load full document (needed to access Password fields)
	doc = frappe.get_doc("DD Settings", settings.name)
	mode = doc.integration_mode

	if mode == "Direct Mode":
		return HubtelService(doc)
	elif mode == "KolectPay Mode":
		return KolectPayBridge(doc)
	else:
		frappe.throw(
			f"Unknown integration mode '{mode}' in DD Settings for '{company}'.",
			title="DD Configuration Error",
		)
