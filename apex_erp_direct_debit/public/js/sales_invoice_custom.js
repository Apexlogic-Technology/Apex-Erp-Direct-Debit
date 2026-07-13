/**
 * sales_invoice_custom.js
 * Adds Direct Debit controls to the ERPNext Sales Invoice form.
 *
 * Buttons added (on submitted invoices with DD configured):
 *   - Create DD Debt       → dialog to set up installment terms
 *   - Trigger Debit Now    → manual debit of next pending installment
 *   - DD Collection Status → shows installment schedule inline
 */

frappe.ui.form.on("Sales Invoice", {
  refresh(frm) {
    if (frm.doc.docstatus !== 1) return;         // Submitted only
    if (!(frm.doc.outstanding_amount > 0)) return; // Only if still outstanding

    const company = frm.doc.company;

    // Check if DD is configured for this company
    frappe.db.exists("DD Settings", company).then((exists) => {
      if (!exists) return;

      // ── Create DD Debt ─────────────────────────────────────────────────
      const hasActiveDebt = frm.doc.__onload && frm.doc.__onload.active_dd_debt;
      if (!hasActiveDebt) {
        frm.add_custom_button(
          __("Create DD Debt"),
          () => _create_dd_debt_dialog(frm),
          __("Direct Debit")
        );
      } else {
        // ── Trigger Debit Now ────────────────────────────────────────────
        frm.add_custom_button(
          __("Trigger Debit Now"),
          () => _trigger_debit_now(frm, hasActiveDebt),
          __("Direct Debit")
        );

        // ── DD Collection Status ─────────────────────────────────────────
        frm.add_custom_button(
          __("DD Collection Status"),
          () => frappe.set_route("Form", "DD Debt", hasActiveDebt),
          __("Direct Debit")
        );
      }
    });
  },

  onload_post_render(frm) {
    if (frm.doc.docstatus !== 1) return;
    // Check for existing active DD Debt for this invoice
    frappe.call({
      method: "frappe.client.get_list",
      args: {
        doctype: "DD Debt",
        filters: {
          sales_invoice: frm.doc.name,
          debt_status: ["in", ["Draft", "Active"]],
        },
        fields: ["name"],
        limit: 1,
      },
      callback(r) {
        if (r.message && r.message.length > 0) {
          frm.doc.__onload = frm.doc.__onload || {};
          frm.doc.__onload.active_dd_debt = r.message[0].name;
          frm.refresh();
        }
      },
    });
  },
});

// ─────────────────────────────────────────────────────────────────────────────
// Create DD Debt dialog
// ─────────────────────────────────────────────────────────────────────────────

function _create_dd_debt_dialog(frm) {
  const d = new frappe.ui.Dialog({
    title: __("Create DD Debt — Installment Setup"),
    size: "large",
    fields: [
      {
        label: __("Collection Type"),
        fieldname: "collection_type",
        fieldtype: "Select",
        options: "One-Time\nInstallment\nSubscription",
        default: "Installment",
        reqd: 1,
      },
      {
        label: __("Frequency"),
        fieldname: "frequency",
        fieldtype: "Select",
        options: "Daily\nWeekly\nMonthly",
        default: "Monthly",
        depends_on: "eval:in(['Installment','Subscription'], doc.collection_type)",
        reqd: 1,
      },
      {
        fieldtype: "Column Break",
      },
      {
        label: __("Number of Installments"),
        fieldname: "num_installments",
        fieldtype: "Int",
        default: 12,
        description: __("e.g. 12 for a 12-month plan"),
        depends_on: "eval:doc.collection_type=='Installment'",
        reqd: 1,
      },
      {
        label: __("Start Date"),
        fieldname: "start_date",
        fieldtype: "Date",
        default: frappe.datetime.get_today(),
        reqd: 1,
      },
      {
        fieldtype: "Section Break",
        label: __("Details"),
      },
      {
        label: __("Total Amount to Collect"),
        fieldname: "total_amount",
        fieldtype: "Currency",
        default: frm.doc.outstanding_amount,
        description: __("Defaults to invoice outstanding amount"),
        reqd: 1,
        read_only: 1,
      },
      {
        label: __("Description / Narration"),
        fieldname: "description",
        fieldtype: "Small Text",
        default: `Installment collection for Invoice ${frm.doc.name}`,
      },
    ],
    primary_action_label: __("Create DD Debt"),
    primary_action(values) {
      d.hide();
      frappe.call({
        method: "apex_erp_direct_debit.doctype_changes.sales_invoice_custom.create_dd_debt",
        args: {
          invoice_name:    frm.doc.name,
          collection_type: values.collection_type,
          frequency:       values.frequency || "",
          num_installments: values.num_installments || 1,
          start_date:      values.start_date,
          description:     values.description || "",
        },
        callback(r) {
          if (r.message && r.message.success) {
            frappe.show_alert({
              message: r.message.message,
              indicator: "green",
            });
            // Open the created DD Debt
            frappe.set_route("Form", "DD Debt", r.message.debt);
          } else {
            frappe.msgprint({
              title: __("Error"),
              message: r.message ? r.message.message : __("Failed to create DD Debt."),
              indicator: "red",
            });
          }
        },
      });
    },
  });
  d.show();
}

// ─────────────────────────────────────────────────────────────────────────────
// Trigger Debit Now
// ─────────────────────────────────────────────────────────────────────────────

function _trigger_debit_now(frm, debtName) {
  frappe.confirm(
    __("Trigger an immediate debit for the next pending installment on DD Debt {0}?", [debtName]),
    () => {
      frappe.call({
        method: "apex_erp_direct_debit.api.mandate.trigger_debit_now",
        args: { debt_name: debtName },
        callback(r) {
          if (r.message && r.message.success) {
            frappe.show_alert({
              message: __(
                "Debit triggered. Transaction: {0}. Awaiting gateway callback.",
                [r.message.transaction]
              ),
              indicator: "green",
            });
          } else {
            frappe.msgprint({
              title: __("Debit Failed"),
              message: r.message ? r.message.message : __("Trigger failed."),
              indicator: "red",
            });
          }
        },
      });
    }
  );
}
