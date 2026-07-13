import frappe
from frappe.model.document import Document


class DDWebhookLog(Document):
	"""Raw webhook log — always written before processing to ensure auditability."""
	pass
