frappe.views.listview_settings['DD Debt'] = {
	onload(listview) {
		listview.page.add_inner_button(__('Bulk Pause'), () => {
			const checked = listview.get_checked_items();
			if (!checked.length) {
				frappe.msgprint(__('Select at least one record.'));
				return;
			}
			frappe.confirm(
				__('Are you sure you want to pause {0} selected collections?', [checked.length]),
				() => {
					frappe.call({
						method: 'apex_erp_direct_debit.direct_debit.doctype.dd_debt.dd_debt.bulk_pause_debts',
						args: {
							names: checked.map(c => c.name)
						},
						callback() {
							listview.refresh();
							frappe.show_alert({ message: __('Selected collections paused.'), indicator: 'orange' });
						}
					});
				}
			);
		}, __('Actions'));

		listview.page.add_inner_button(__('Bulk Resume'), () => {
			const checked = listview.get_checked_items();
			if (!checked.length) {
				frappe.msgprint(__('Select at least one record.'));
				return;
			}
			frappe.confirm(
				__('Are you sure you want to resume {0} selected collections?', [checked.length]),
				() => {
					frappe.call({
						method: 'apex_erp_direct_debit.direct_debit.doctype.dd_debt.dd_debt.bulk_resume_debts',
						args: {
							names: checked.map(c => c.name)
						},
						callback() {
							listview.refresh();
							frappe.show_alert({ message: __('Selected collections resumed.'), indicator: 'green' });
						}
					});
				}
			);
		}, __('Actions'));
	}
};
