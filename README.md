# Apex ERP Direct Debit

A production-grade ERPNext v15+ custom app that integrates Hubtel Direct Debit
into ERPNext, supporting both a Bridge mode (KolectPay Business / SMCollect) and
a Direct mode (Hubtel Pre-Approval API).

---

## Features

- **Two Operating Modes**
  - **Bridge – KolectPay Business**: ERPNext calls KolectPay's REST API
  - **Bridge – SMCollect**: ERPNext calls SMCollect's REST API
  - **Direct – Hubtel**: ERPNext calls Hubtel Pre-Approval + Receive Money APIs directly

- **Installment / Loan Collections**
  - Auto-generate installment schedules from Sales Invoices
  - Scheduler auto-triggers due debits
  - Payment Entries auto-created on successful debit → Sales Invoice outstanding reduced

- **Full Mandate Lifecycle**
  - Initiate → USSD/OTP approval → Active → Debit → Callback → Payment Entry

- **Per-Company Settings**
  - Each ERPNext Company can have its own DD Settings (different mode, different credentials)

- **Webhook Endpoint**
  - Receives Hubtel mandate + debit callbacks securely (HMAC-verified)

---

## Installation

```bash
# From your bench directory
bench get-app /path/to/"Apex Erp Direct Debit"
bench install-app apex_erp_direct_debit
bench migrate
bench restart
```

---

## Configuration

1. Go to **DD Settings** → create a record for your Company
2. Select **Integration Mode**
3. Fill in credentials (Hubtel or Bridge URL/token)
4. Enable the app

---

## License

MIT
