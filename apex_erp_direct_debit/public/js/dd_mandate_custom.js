/**
 * dd_mandate_custom.js
 * Action buttons on the DD Mandate form.
 * Mirrors Customer form actions but accessible directly from the Mandate record.
 */

frappe.ui.form.on("DD Mandate", {
  refresh(frm) {
    frm.page.set_indicator(
      frm.doc.mandate_status,
      {
        Draft:     "gray",
        Pending:   "yellow",
        Approved:  "green",
        Failed:    "red",
        Cancelled: "orange",
        Expired:   "gray",
      }[frm.doc.mandate_status] || "gray"
    );

    const s = frm.doc.mandate_status;

    // ── Initiate ─────────────────────────────────────────────────────────────
    if (["Draft", "Failed", "Expired"].includes(s)) {
      frm.add_custom_button(__("Initiate Mandate"), () => {
        frappe.confirm(
          __("Send pre-approval request to Hubtel for this mandate?"),
          () => {
            frappe.show_progress(__("Initiating…"), 50, 100);
            frappe.call({
              method: "apex_erp_direct_debit.api.mandate.initiate_mandate",
              args: { mandate_name: frm.doc.name },
              callback(r) {
                frappe.hide_progress();
                if (r.message && r.message.success) {
                  const msg = r.message.verification_type === "OTP"
                    ? __("OTP sent to customer. Use 'Verify OTP' to complete.")
                    : __("USSD prompt sent. Waiting for customer approval.");
                  frappe.show_alert({ message: msg, indicator: "green" });
                  frm.reload_doc();
                } else {
                  frappe.msgprint({
                    title: __("Initiation Failed"),
                    message: (r.message && r.message.message) || __("Unknown error"),
                    indicator: "red",
                  });
                }
              },
            });
          }
        );
      });
    }

    // ── Verify OTP ────────────────────────────────────────────────────────────
    if (s === "Pending" && frm.doc.verification_type === "OTP") {
      frm.add_custom_button(__("Verify OTP"), () => {
        const d = new frappe.ui.Dialog({
          title: __("Enter Customer OTP"),
          fields: [{
            label: __("OTP Code"),
            fieldname: "otp_code",
            fieldtype: "Data",
            description: __("e.g. HNRM-1234 or just 1234"),
            reqd: 1,
          }],
          primary_action_label: __("Verify"),
          primary_action({ otp_code }) {
            d.hide();
            frappe.call({
              method: "apex_erp_direct_debit.api.mandate.verify_otp",
              args: { mandate_name: frm.doc.name, otp_code },
              callback(r) {
                if (r.message && r.message.success) {
                  frappe.show_alert({ message: __("OTP verified."), indicator: "green" });
                  frm.reload_doc();
                } else {
                  frappe.msgprint({
                    title: __("OTP Failed"),
                    message: (r.message && r.message.message) || __("Verification failed."),
                    indicator: "red",
                  });
                }
              },
            });
          },
        });
        d.show();
      });
    }

    // ── Check Status ──────────────────────────────────────────────────────────
    if (["Pending", "Approved"].includes(s)) {
      frm.add_custom_button(__("Check Gateway Status"), () => {
        frappe.call({
          method: "apex_erp_direct_debit.api.mandate.check_mandate_status",
          args: { mandate_name: frm.doc.name },
          callback(r) {
            if (r.message) {
              const resp = r.message.response || {};
              const gStatus = (resp.data && resp.data.preapprovalStatus) || resp.preapprovalStatus || "Unknown";
              frappe.msgprint({
                title: __("Gateway Status: {0}", [gStatus]),
                message: `<pre style="font-size:11px">${JSON.stringify(resp, null, 2)}</pre>`,
                indicator: gStatus === "APPROVED" ? "green" : "orange",
              });
              frm.reload_doc();
            }
          },
        });
      });
    }

    // ── Cancel ────────────────────────────────────────────────────────────────
    if (["Pending", "Approved"].includes(s)) {
      frm.add_custom_button(__("Cancel Mandate"), () => {
        frappe.confirm(__("Cancel this Direct Debit mandate?"), () => {
          frappe.call({
            method: "apex_erp_direct_debit.api.mandate.cancel_mandate",
            args: { mandate_name: frm.doc.name },
            callback(r) {
              if (r.message && r.message.success) {
                frappe.show_alert({ message: __("Mandate cancelled."), indicator: "orange" });
                frm.reload_doc();
              } else {
                frappe.msgprint({ title: __("Error"), message: r.message.message, indicator: "red" });
              }
            },
          });
        });
      }, __("Actions"));
    }

    // ── Reactivate ────────────────────────────────────────────────────────────
    if (s === "Cancelled") {
      frm.add_custom_button(__("Reactivate Mandate"), () => {
        frappe.confirm(__("Send a reactivation request to Hubtel?"), () => {
          frappe.call({
            method: "apex_erp_direct_debit.api.mandate.reactivate_mandate",
            args: { mandate_name: frm.doc.name },
            callback(r) {
              if (r.message && r.message.success) {
                frappe.show_alert({ message: __("Reactivation initiated."), indicator: "green" });
                frm.reload_doc();
              } else {
                frappe.msgprint({ title: __("Error"), message: r.message.message, indicator: "red" });
              }
            },
          });
        });
      }, __("Actions"));
    }

    // ── View linked debts ─────────────────────────────────────────────────────
    frm.add_custom_button(__("View Linked Debts"), () => {
      frappe.set_route("List", "DD Debt", { mandate: frm.doc.name });
    });
  },
});
