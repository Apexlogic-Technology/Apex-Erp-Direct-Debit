/**
 * customer_custom.js
 * Adds Direct Debit action buttons and OTP dialog to the ERPNext Customer form.
 *
 * Buttons added:
 *   - Initiate Mandate
 *   - Verify OTP (shown only when mandate.verification_type == 'OTP' and status == 'Pending')
 *   - Cancel Mandate
 *   - Reactivate Mandate
 *   - Check Status
 *   - View DD Dashboard (opens DD Debt list)
 */

frappe.ui.form.on("Customer", {
  refresh(frm) {
    // Only show DD buttons if the customer has a mobile number configured
    if (!frm.doc.dd_mobile_number) return;

    const mandateName = frm.doc.dd_mandate;
    const mandateStatus = frm.doc.dd_mandate_status;

    frm.add_custom_button(
      __("View DD Dashboard"),
      () => frappe.set_route("List", "DD Debt", { customer: frm.doc.name }),
      __("Direct Debit")
    );

    // ── Initiate Mandate ──────────────────────────────────────────────────
    if (!mandateName || ["Draft", "Failed", "Expired"].includes(mandateStatus)) {
      frm.add_custom_button(
        __("Initiate Mandate"),
        () => _initiate_mandate(frm),
        __("Direct Debit")
      );
    }

    // ── Verify OTP ───────────────────────────────────────────────────────
    if (mandateName && mandateStatus === "Pending") {
      frm.add_custom_button(
        __("Verify OTP"),
        () => _verify_otp_dialog(frm, mandateName),
        __("Direct Debit")
      );
    }

    // ── Cancel Mandate ───────────────────────────────────────────────────
    if (mandateStatus && ["Pending", "Approved"].includes(mandateStatus)) {
      frm.add_custom_button(
        __("Cancel Mandate"),
        () => _cancel_mandate(frm, mandateName),
        __("Direct Debit")
      );
    }

    // ── Reactivate Mandate ───────────────────────────────────────────────
    if (mandateStatus === "Cancelled") {
      frm.add_custom_button(
        __("Reactivate Mandate"),
        () => _reactivate_mandate(frm, mandateName),
        __("Direct Debit")
      );
    }

    // ── Check Status ─────────────────────────────────────────────────────
    if (mandateName && ["Pending", "Approved"].includes(mandateStatus)) {
      frm.add_custom_button(
        __("Check Status"),
        () => _check_status(frm, mandateName),
        __("Direct Debit")
      );
    }
  },

  dd_mobile_number(frm) {
    // Auto-detect channel from phone prefix when mobile number is typed
    const mobile = (frm.doc.dd_mobile_number || "").replace(/\D/g, "");
    if (!mobile) return;

    const local = mobile.startsWith("233") ? "0" + mobile.slice(3) : mobile;
    const mtn         = ["024", "054", "055", "059"];
    const vodafone    = ["020", "050"];
    const airteltigo  = ["027", "057", "026", "056", "023", "053"];

    if (mtn.some(p => local.startsWith(p))) {
      frm.set_value("dd_channel", "MTN");
    } else if (vodafone.some(p => local.startsWith(p))) {
      frm.set_value("dd_channel", "Vodafone");
    } else if (airteltigo.some(p => local.startsWith(p))) {
      frm.set_value("dd_channel", "AirtelTigo");
    }
  },
});

// ─────────────────────────────────────────────────────────────────────────────
// Action helpers
// ─────────────────────────────────────────────────────────────────────────────

function _initiate_mandate(frm) {
  // First create (or find) a DD Mandate for this customer, then initiate it
  frappe.confirm(
    __("Initiate a Direct Debit mandate for {0} ({1}, {2})?", [
      frm.doc.customer_name,
      frm.doc.dd_mobile_number,
      frm.doc.dd_channel || "Auto-detect",
    ]),
    () => {
      frappe.show_progress(__("Initiating Mandate..."), 0, 100, __("Please wait"));

      // Create a DD Mandate document first (if one doesn't exist already)
      _ensure_mandate(frm)
        .then((mandateName) => {
          return frappe.call({
            method: "apex_erp_direct_debit.api.mandate.initiate_mandate",
            args: { mandate_name: mandateName },
          });
        })
        .then((r) => {
          frappe.hide_progress();
          if (r.message && r.message.success) {
            const vtype = r.message.verification_type;
            if (vtype === "OTP") {
              frappe.msgprint({
                title: __("OTP Sent"),
                message: __(
                  "An OTP has been sent to the customer. Ask them for the code and use "
                  + "<b>Verify OTP</b> to complete mandate setup."
                ),
                indicator: "green",
              });
            } else {
              frappe.msgprint({
                title: __("USSD Prompt Sent"),
                message: __("The customer should approve the USSD prompt on their phone."),
                indicator: "blue",
              });
            }
            frm.reload_doc();
          } else {
            frappe.msgprint({
              title: __("Mandate Initiation Failed"),
              message: r.message ? r.message.message : __("Unknown error"),
              indicator: "red",
            });
          }
        })
        .catch(() => frappe.hide_progress());
    }
  );
}

function _verify_otp_dialog(frm, mandateName) {
  const d = new frappe.ui.Dialog({
    title: __("Verify OTP"),
    fields: [
      {
        label: __("OTP Code"),
        fieldname: "otp_code",
        fieldtype: "Data",
        description: __(
          "Enter the OTP the customer received (e.g. HNRM-1234 or just 1234)"
        ),
        reqd: 1,
      },
    ],
    primary_action_label: __("Verify"),
    primary_action({ otp_code }) {
      d.hide();
      frappe.call({
        method: "apex_erp_direct_debit.api.mandate.verify_otp",
        args: { mandate_name: mandateName, otp_code },
        callback(r) {
          if (r.message && r.message.success) {
            frappe.show_alert({ message: __("OTP verified successfully."), indicator: "green" });
            frm.reload_doc();
          } else {
            frappe.msgprint({
              title: __("OTP Verification Failed"),
              message: r.message ? r.message.message : __("Verification failed."),
              indicator: "red",
            });
          }
        },
      });
    },
  });
  d.show();
}

function _cancel_mandate(frm, mandateName) {
  frappe.confirm(__("Cancel the Direct Debit mandate for this customer?"), () => {
    frappe.call({
      method: "apex_erp_direct_debit.api.mandate.cancel_mandate",
      args: { mandate_name: mandateName },
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
}

function _reactivate_mandate(frm, mandateName) {
  frappe.confirm(__("Reactivate the cancelled mandate?"), () => {
    frappe.call({
      method: "apex_erp_direct_debit.api.mandate.reactivate_mandate",
      args: { mandate_name: mandateName },
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
}

function _check_status(frm, mandateName) {
  frappe.call({
    method: "apex_erp_direct_debit.api.mandate.check_mandate_status",
    args: { mandate_name: mandateName },
    callback(r) {
      if (r.message) {
        const resp = r.message.response || {};
        const status = resp.data ? resp.data.preapprovalStatus : resp.preapprovalStatus || "Unknown";
        frappe.msgprint({
          title: __("Mandate Status"),
          message: `<b>Gateway Status:</b> ${status}<br><pre>${JSON.stringify(resp, null, 2)}</pre>`,
          indicator: status === "APPROVED" ? "green" : "orange",
        });
        frm.reload_doc();
      }
    },
  });
}

function _ensure_mandate(frm) {
  /**
   * Find an existing non-completed mandate for this customer,
   * or create a new Draft one, then return its name.
   */
  return frappe.call({
    method: "frappe.client.get_list",
    args: {
      doctype: "DD Mandate",
      filters: {
        customer: frm.doc.name,
        mandate_status: ["in", ["Draft", "Pending", "Failed", "Expired"]],
      },
      fields: ["name", "mandate_status"],
      limit: 1,
    },
  }).then((r) => {
    if (r.message && r.message.length > 0) {
      return r.message[0].name;
    }
    // Create new mandate
    return frappe.call({
      method: "frappe.client.insert",
      args: {
        doc: {
          doctype: "DD Mandate",
          company: frappe.defaults.get_default("company"),
          customer: frm.doc.name,
          mobile_number: frm.doc.dd_mobile_number,
          channel: frm.doc.dd_channel || "",
          mandate_status: "Draft",
        },
      },
    }).then((r2) => {
      // Link mandate back to customer
      frm.set_value("dd_mandate", r2.message.name);
      return frm.save().then(() => r2.message.name);
    });
  });
}
