"""
Payment Entry doctype hooks.

before_insert: If this PE was auto-created by the webhook handler (linked to a
               DD Transaction), set the mode_of_payment and add a narration suffix.
after_insert:  Link the PE back to any DD Transaction that references it
               (additional cross-reference guard).
"""

import frappe


def before_insert(doc, method=None):
	"""Set Mobile Money - Direct Debit as mode of payment for DD-originated entries."""
	if not _is_dd_payment(doc):
		return
	if not doc.mode_of_payment:
		doc.mode_of_payment = "Mobile Money - Direct Debit"
	if doc.remarks and "[DD]" not in doc.remarks:
		doc.remarks += " [Direct Debit]"


def after_insert(doc, method=None):
	"""No additional action needed — linking is done in webhook.py."""
	pass


def _is_dd_payment(doc) -> bool:
	"""
	Detect if this PE was created by the DD webhook (the remarks field
	contains 'DD Transaction:' or 'Direct Debit collection via').
	"""
	return bool(
		doc.remarks
		and (
			"DD Transaction:" in (doc.remarks or "")
			or "Direct Debit collection via" in (doc.remarks or "")
		)
	)
