import frappe
from frappe.model.document import Document
from frappe.utils import flt, formatdate


class DDReconciliationReport(Document):
	def validate(self):
		self.calculate_metrics()

	def calculate_metrics(self):
		if not self.company or not self.period_start or not self.period_end:
			return

		txns = frappe.get_all(
			"DD Transaction",
			filters={
				"company": self.company,
				"initiated_at": ["between", [str(self.period_start) + " 00:00:00", str(self.period_end) + " 23:59:59"]],
			},
			fields=["name", "debt", "mandate", "status", "amount", "charge", "initiated_at", "gateway_txn_id", "client_reference_id"],
		)

		self.total_debits_attempted = len(txns)
		self.total_debits_success = sum(1 for t in txns if t.status == "Success")
		self.total_debits_failed = sum(1 for t in txns if t.status == "Failed")
		self.total_amount_collected = sum(flt(t.amount) for t in txns if t.status == "Success")
		self.total_gateway_charges = sum(flt(t.charge) for t in txns if t.status == "Success")
		self.unmatched_transactions = sum(1 for t in txns if t.status == "Inconclusive")
		self.report_data = frappe.as_json(txns)
		self.report_html = _build_report_html(txns, self)


# ─────────────────────────────────────────────────────────────────────────────
# HTML Report Builder
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_BADGE = {
	"Success":     "background:#d1fae5;color:#065f46;",
	"Failed":      "background:#fee2e2;color:#991b1b;",
	"Pending":     "background:#fef3c7;color:#92400e;",
	"Inconclusive":"background:#e0e7ff;color:#3730a3;",
}


def _build_report_html(txns: list, doc) -> str:
	"""Render a beautiful HTML summary table for the Reconciliation Report."""

	# ── Summary cards ──
	net_collected = flt(doc.total_amount_collected) - flt(doc.total_gateway_charges)
	success_rate = (
		round(100 * doc.total_debits_success / doc.total_debits_attempted, 1)
		if doc.total_debits_attempted else 0
	)

	cards_html = f"""
	<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px;">
		{_card("Total Attempted", doc.total_debits_attempted, "#3b82f6")}
		{_card("Successful", doc.total_debits_success, "#10b981")}
		{_card("Failed", doc.total_debits_failed, "#ef4444")}
		{_card("Inconclusive", doc.unmatched_transactions, "#8b5cf6")}
		{_card("Amount Collected", f"GHS {flt(doc.total_amount_collected):,.2f}", "#059669")}
		{_card("Gateway Charges", f"GHS {flt(doc.total_gateway_charges):,.2f}", "#f59e0b")}
		{_card("Net Collected", f"GHS {net_collected:,.2f}", "#0ea5e9")}
		{_card("Success Rate", f"{success_rate}%", "#6366f1")}
	</div>
	"""

	# ── Transaction table ──
	if not txns:
		table_html = '<p style="color:#6b7280;margin-top:16px;">No transactions in this period.</p>'
	else:
		rows = ""
		for i, t in enumerate(txns):
			badge_style = _STATUS_BADGE.get(t.status, "background:#f3f4f6;color:#374151;")
			rows += f"""
			<tr style="background:{'#f9fafb' if i % 2 == 0 else '#ffffff'};">
				<td style="padding:10px 14px;font-size:12px;color:#374151;">{t.name}</td>
				<td style="padding:10px 14px;font-size:12px;color:#374151;">{t.debt or "—"}</td>
				<td style="padding:10px 14px;font-size:12px;color:#374151;">{t.mandate or "—"}</td>
				<td style="padding:10px 14px;text-align:right;font-size:12px;color:#111827;font-weight:600;">
					GHS {flt(t.amount):,.2f}
				</td>
				<td style="padding:10px 14px;text-align:right;font-size:12px;color:#6b7280;">
					{f'GHS {flt(t.charge):,.2f}' if t.charge else '—'}
				</td>
				<td style="padding:10px 14px;text-align:center;">
					<span style="padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;{badge_style}">
						{t.status}
					</span>
				</td>
				<td style="padding:10px 14px;font-size:11px;color:#6b7280;">{t.gateway_txn_id or "—"}</td>
				<td style="padding:10px 14px;font-size:11px;color:#6b7280;">
					{str(t.initiated_at)[:16] if t.initiated_at else "—"}
				</td>
			</tr>
			"""

		table_html = f"""
		<div style="overflow-x:auto;border-radius:10px;border:1px solid #e5e7eb;">
		<table style="width:100%;border-collapse:collapse;font-family:'Inter',sans-serif;">
			<thead>
				<tr style="background:linear-gradient(135deg,#1e3a8a,#3b82f6);color:#fff;">
					<th style="padding:12px 14px;text-align:left;font-size:12px;font-weight:600;">Transaction</th>
					<th style="padding:12px 14px;text-align:left;font-size:12px;font-weight:600;">Debt</th>
					<th style="padding:12px 14px;text-align:left;font-size:12px;font-weight:600;">Mandate</th>
					<th style="padding:12px 14px;text-align:right;font-size:12px;font-weight:600;">Amount</th>
					<th style="padding:12px 14px;text-align:right;font-size:12px;font-weight:600;">Charge</th>
					<th style="padding:12px 14px;text-align:center;font-size:12px;font-weight:600;">Status</th>
					<th style="padding:12px 14px;text-align:left;font-size:12px;font-weight:600;">Gateway ID</th>
					<th style="padding:12px 14px;text-align:left;font-size:12px;font-weight:600;">Initiated</th>
				</tr>
			</thead>
			<tbody>{rows}</tbody>
		</table>
		</div>
		"""

	period_label = f"{formatdate(doc.period_start)} – {formatdate(doc.period_end)}"
	return f"""
	<div style="font-family:'Inter',sans-serif;padding:16px;">
		<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;">
			<div>
				<h2 style="margin:0;font-size:20px;font-weight:700;color:#1e3a8a;">
					📊 Direct Debit Reconciliation Report
				</h2>
				<p style="margin:4px 0 0;color:#6b7280;font-size:13px;">
					{doc.company} &nbsp;|&nbsp; Period: {period_label}
				</p>
			</div>
		</div>
		{cards_html}
		<h3 style="margin:0 0 12px;font-size:15px;font-weight:600;color:#374151;">Transaction Detail</h3>
		{table_html}
	</div>
	"""


def _card(label: str, value, color: str) -> str:
	return f"""
	<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;
				padding:14px 18px;min-width:130px;flex:1;box-shadow:0 1px 3px rgba(0,0,0,.06);">
		<p style="margin:0;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af;">{label}</p>
		<p style="margin:6px 0 0;font-size:20px;font-weight:700;color:{color};">{value}</p>
	</div>
	"""
