import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class DDNotificationLog(Document):
	@staticmethod
	def log_notification(company: str, customer: str, contact_number: str,
						 channel: str, message_type: str, message: str,
						 status: str, error_details: str = None) -> str:
		"""Helper static method to record notification outputs in background threads."""
		doc = frappe.get_doc({
			"doctype":        "DD Notification Log",
			"company":        company,
			"customer":       customer,
			"contact_number": contact_number,
			"channel":        channel,
			"message_type":   message_type,
			"message":        message,
			"sent_at":        now_datetime(),
			"status":         status,
			"error_details":  error_details,
		})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
