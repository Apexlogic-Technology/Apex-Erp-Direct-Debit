/**
 * dd_debt_custom.js
 * Action buttons on the DD Debt form.
 *
 * Provides:
 *   - Trigger Debit Now  (full or partial amount)
 *   - Pause / Resume Debt
 *   - View Transactions
 *   - Installment row quick-actions (Skip row)
 */

frappe.ui.form.on("DD Debt", {
  setup(frm) {
    frm.set_query("mandate", () => {
      return {
        filters: {
          customer: frm.doc.customer || "",
          mandate_status: "Approved",
        },
      };
    });
  },

  refresh(frm) {
    const status = frm.doc.debt_status;

    // Status indicator badge
    frm.page.set_indicator(
      status,
      {
        Draft:     "gray",
        Active:    "green",
        Paused:    "orange",
        Completed: "blue",
        Cancelled: "red",
        Defaulted: "red",
      }[status] || "gray"
    );

    // ── Trigger Debit Now ─────────────────────────────────────────────────
    if (status === "Active") {
      frm.add_custom_button(__("Trigger Debit Now"), () => {
        _trigger_debit_dialog(frm);
      }, __("Actions"));
    }

    // ── Pause / Resume ────────────────────────────────────────────────────
    if (status === "Active") {
      frm.add_custom_button(__("Pause Collections"), () => {
        frappe.confirm(
          __("Pause all future automatic debits for this debt?"),
          () => {
            frappe.db.set_value("DD Debt", frm.doc.name, "debt_status", "Paused").then(() => {
              frappe.show_alert({ message: __("Debt paused."), indicator: "orange" });
              frm.reload_doc();
            });
          }
        );
      }, __("Actions"));
    }

    if (status === "Paused") {
      frm.add_custom_button(__("Resume Collections"), () => {
        frappe.confirm(__("Resume automatic debits for this debt?"), () => {
          frappe.db.set_value("DD Debt", frm.doc.name, "debt_status", "Active").then(() => {
            frappe.show_alert({ message: __("Debt resumed."), indicator: "green" });
            frm.reload_doc();
          });
        });
      }, __("Actions"));
    }

    // ── View Transactions ─────────────────────────────────────────────────
    frm.add_custom_button(__("View Transactions"), () => {
      frappe.set_route("List", "DD Transaction", { debt: frm.doc.name });
    });

    // ── Highlight overdue installments ────────────────────────────────────
    _highlight_overdue_rows(frm);
  },
});


// ─────────────────────────────────────────────────────────────────────────────
// Trigger Debit Dialog (supports partial amount)
// ─────────────────────────────────────────────────────────────────────────────

function _trigger_debit_dialog(frm) {
  // Find the next pending installment for display purposes
  const pendingRows = (frm.doc.installment_schedule || []).filter(r => r.status === "Pending");
  const nextRow = pendingRows.sort((a, b) => new Date(a.due_date) - new Date(b.due_date))[0];
  const defaultAmount = nextRow ? nextRow.installment_amount : 0;

  const d = new frappe.ui.Dialog({
    title: __("Trigger Debit"),
    size: "small",
    fields: [
      {
        label: __("Debit Amount (GHS)"),
        fieldname: "amount",
        fieldtype: "Currency",
        default: defaultAmount,
        description: __(
          "Default: next installment amount ({0}). Enter a smaller value for a partial debit.",
          [frappe.format(defaultAmount, { fieldtype: "Currency" })]
        ),
        reqd: 1,
      },
      {
        label: __("Note"),
        fieldname: "note",
        fieldtype: "HTML",
        options: nextRow
          ? `<div class="alert alert-info" style="font-size:12px;">
               Next due: <b>${frappe.datetime.str_to_user(nextRow.due_date)}</b>
               &nbsp;|&nbsp; Full amount: <b>GHS ${frappe.format(defaultAmount, {fieldtype:"Currency"})}</b>
             </div>`
          : `<div class="alert alert-warning">No pending installments found.</div>`,
      },
    ],
    primary_action_label: __("Debit Now"),
    primary_action({ amount }) {
      if (!amount || amount <= 0) {
        frappe.msgprint(__("Please enter a valid amount greater than zero."));
        return;
      }
      if (nextRow && amount > nextRow.installment_amount) {
        frappe.msgprint(__(
          "Amount ({0}) cannot exceed the installment amount ({1}).",
          [amount, nextRow.installment_amount]
        ));
        return;
      }
      d.hide();
      frappe.show_progress(__("Triggering debit…"), 50, 100);
      frappe.call({
        method: "apex_erp_direct_debit.api.mandate.trigger_debit_now",
        args: {
          debt_name: frm.doc.name,
          custom_amount: amount !== defaultAmount ? amount : null,
        },
        callback(r) {
          frappe.hide_progress();
          if (r.message && r.message.success) {
            const isPartial = r.message.is_partial;
            frappe.show_alert({
              message: isPartial
                ? __("Partial debit of GHS {0} triggered. Remainder added as new installment. Txn: {1}",
                    [amount, r.message.transaction])
                : __("Debit triggered. Txn: {0}", [r.message.transaction]),
              indicator: "green",
            });
            frm.reload_doc();
          } else {
            frappe.msgprint({
              title: __("Debit Failed"),
              message: (r.message && r.message.message) || __("Trigger failed."),
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
// Visual helpers
// ─────────────────────────────────────────────────────────────────────────────

function _highlight_overdue_rows(frm) {
  /**
   * After render, colour overdue Pending installment rows red
   * and upcoming rows within 3 days orange.
   */
  const today = frappe.datetime.get_today();
  const soon = frappe.datetime.add_days(today, 3);

  setTimeout(() => {
    (frm.doc.installment_schedule || []).forEach((row, idx) => {
      if (row.status !== "Pending") return;
      const due = row.due_date;
      const rowEl = frm.fields_dict.installment_schedule
        && frm.fields_dict.installment_schedule.grid
        && frm.fields_dict.installment_schedule.grid.grid_rows[idx]
        && frm.fields_dict.installment_schedule.grid.grid_rows[idx].row;

      if (!rowEl) return;
      if (due < today) {
        rowEl.style.background = "#fef2f2"; // soft red — overdue
      } else if (due <= soon) {
        rowEl.style.background = "#fffbeb"; // soft amber — due soon
      }
    });
  }, 400);
}
