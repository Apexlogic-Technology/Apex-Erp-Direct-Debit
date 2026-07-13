"""
Base class for all Direct Debit provider implementations.
Both Bridge providers and the Direct Hubtel provider implement this interface.
"""
from abc import ABC, abstractmethod


class DirectDebitProviderBase(ABC):
	"""Abstract base class that every DD provider must implement."""

	def __init__(self, settings):
		self.settings = settings

	# ─── Mandate (Pre-Approval) ───────────────────────────────────────────

	@abstractmethod
	def initiate_mandate(self, mandate_doc) -> dict:
		"""
		Initiate a new mandate / pre-approval for a customer.
		Returns the raw API response dict.
		"""
		raise NotImplementedError

	@abstractmethod
	def verify_otp(self, mandate_doc, otp_code: str) -> dict:
		"""
		Submit OTP verification for the mandate (OTP flow only).
		Returns the raw API response dict.
		"""
		raise NotImplementedError

	@abstractmethod
	def cancel_mandate(self, mandate_doc) -> dict:
		"""Cancel / revoke an active mandate."""
		raise NotImplementedError

	@abstractmethod
	def reactivate_mandate(self, mandate_doc) -> dict:
		"""Reactivate a previously cancelled mandate."""
		raise NotImplementedError

	@abstractmethod
	def check_mandate_status(self, mandate_doc) -> dict:
		"""Poll the current status of a mandate from the gateway."""
		raise NotImplementedError

	# ─── Debit / Charge ──────────────────────────────────────────────────

	@abstractmethod
	def trigger_debit(self, transaction_doc) -> dict:
		"""
		Trigger an actual charge against an approved mandate.
		Returns raw API response; final status comes via webhook callback.
		"""
		raise NotImplementedError

	@abstractmethod
	def check_transaction_status(self, transaction_doc) -> dict:
		"""Poll the current status of a debit transaction."""
		raise NotImplementedError

	# ─── Bridge-only operations ───────────────────────────────────────────

	def create_debtor(self, customer_doc) -> dict:
		"""Push an ERPNext Customer to the bridge as a Debtor. Bridge mode only."""
		raise NotImplementedError("This provider does not support create_debtor")

	def create_debt(self, debt_doc) -> dict:
		"""Push an ERPNext DD Debt to the bridge. Bridge mode only."""
		raise NotImplementedError("This provider does not support create_debt")

	def sync_transactions(self, company: str, since_datetime: str = None) -> list:
		"""Pull new transactions from the bridge since a given datetime. Bridge mode only."""
		raise NotImplementedError("This provider does not support sync_transactions")
