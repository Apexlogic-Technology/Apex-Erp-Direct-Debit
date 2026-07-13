app_name = "apex_erp_direct_debit"
app_title = "Apex ERP Direct Debit"
app_publisher = "Apex"
app_description = "Hubtel Direct Debit integration for ERPNext — Bridge (KolectPay/SMCollect) and Direct (Hubtel) modes"
app_email = ""
app_license = "mit"

after_install = "apex_erp_direct_debit.install.after_install"
after_migrate = "apex_erp_direct_debit.install.after_migrate"

# ─────────────────────────────────────────────────────────────────────────────
# JS / CSS Includes
# ─────────────────────────────────────────────────────────────────────────────

doctype_js = {
    "Customer":      "public/js/customer_custom.js",
    "Sales Invoice": "public/js/sales_invoice_custom.js",
    "DD Debt":       "public/js/dd_debt_custom.js",
    "DD Mandate":    "public/js/dd_mandate_custom.js",
}

# ─────────────────────────────────────────────────────────────────────────────
# Document Event Hooks
# ─────────────────────────────────────────────────────────────────────────────

doc_events = {
    "Customer": {
        # Auto-push to KolectPay/SMCollect when mobile number is saved (Bridge mode)
        "after_save": "apex_erp_direct_debit.doctype_changes.customer_custom.after_save",
    },
    "Sales Invoice": {
        # When submitted: optionally prompt to create a DD Debt
        "on_submit": "apex_erp_direct_debit.doctype_changes.sales_invoice_custom.on_submit",
        # When cancelled: pause any active DD Debt linked to this invoice
        "on_cancel": "apex_erp_direct_debit.doctype_changes.sales_invoice_custom.on_cancel",
    },
    "Payment Entry": {
        # If PE is linked to a DD Transaction: set Mode of Payment, narration
        "before_insert": "apex_erp_direct_debit.doctype_changes.payment_entry_custom.before_insert",
        "after_insert":  "apex_erp_direct_debit.doctype_changes.payment_entry_custom.after_insert",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Scheduled Tasks
# ─────────────────────────────────────────────────────────────────────────────

scheduler_events = {
    # Runs approximately every 5 minutes (Frappe "all" queue)
    "all": [
        # Direct mode: check status of mandates stuck in "Pending" for > 3 min
        "apex_erp_direct_debit.tasks.poll_pending_mandates",
        # Direct mode: verify debit transactions stuck in "Pending"
        "apex_erp_direct_debit.tasks.poll_pending_transactions",
    ],
    "cron": {
        # Every minute — trigger debits whose installment is due today
        "* * * * *": [
            "apex_erp_direct_debit.tasks.process_due_installments",
        ],
        # Every 15 minutes — pull new transactions from KolectPay/SMCollect (Bridge)
        "*/15 * * * *": [
            "apex_erp_direct_debit.tasks.sync_from_bridge",
        ],
        # Daily 08:00 — pre-debit SMS reminders (Direct mode only)
        "0 8 * * *": [
            "apex_erp_direct_debit.tasks.send_debit_reminders",
        ],
        # Daily 01:00 — generate reconciliation report
        "0 1 * * *": [
            "apex_erp_direct_debit.tasks.generate_reconciliation",
        ],
        # Every 30 minutes — auto-retry failed installments
        "*/30 * * * *": [
            "apex_erp_direct_debit.tasks.retry_failed_installments",
        ],
        # Daily 02:00 — expire mandates past their expiry date
        "0 2 * * *": [
            "apex_erp_direct_debit.tasks.expire_old_mandates",
        ],
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

fixtures = [
    {
        "dt": "Custom Field",
        "filters": {"module": "Apex Erp Direct Debit"},
    },
    {
        "dt": "Property Setter",
        "filters": {"module": "Apex Erp Direct Debit"},
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# Website / API Route Overrides
# ─────────────────────────────────────────────────────────────────────────────

# Webhook endpoint is exposed as a whitelisted method (allow_guest=True):
# POST /api/method/apex_erp_direct_debit.api.webhook.handle_hubtel
