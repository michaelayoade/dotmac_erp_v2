"""Ledger role declarations (PR-1): constants, pins to the other declarations, and pure helpers."""

from uuid import uuid4

from erp_v2 import ledger_roles
from erp_v2.journals import queries
from erp_v2.payroll_payouts import service as payouts
from erp_v2.web import payroll_run_services as run_services


def test_constants_are_the_approved_sets():
    assert ledger_roles.AP_CONTROL_CODE == "2000"
    assert ledger_roles.PAYROLL_PAYABLE_CODE == "2040"
    assert ledger_roles.STAFF_LOAN_CODE == "1410"
    assert ledger_roles.CONTROL_ACCOUNT_CODES == {"2000", "2040", "1410", "1400", "2045"}
    assert ledger_roles.LEDGER_FAULT_CONTROL_CODES == {"2000", "2040", "1410"}  # the fault door is not widened
    assert ledger_roles.INVOICE_TAX_ACCOUNT_CODES == {"1455"}


def test_payroll_and_loan_codes_are_pinned_to_the_payroll_declarations():
    assert ledger_roles.PAYROLL_PAYABLE_CODE == run_services.PAYROLL_PAYABLE_CODE
    assert frozenset({ledger_roles.PAYROLL_PAYABLE_CODE}) == payouts._NET_PAYABLE_CODES.code_refs
    assert frozenset({ledger_roles.STAFF_LOAN_CODE}) == run_services.INTERNAL_RECOVERY_CODES


def test_the_journal_screens_use_the_shared_control_set():
    assert queries.CONTROL_ACCOUNT_CODES is ledger_roles.CONTROL_ACCOUNT_CODES


def test_same_account_compares_by_uuid_value():
    value = uuid4()
    assert ledger_roles.same_account(str(value), value)
    assert ledger_roles.same_account(str(value).upper(), value)
    assert ledger_roles.same_account(value, value)
    assert not ledger_roles.same_account(str(uuid4()), value)
    assert not ledger_roles.same_account("2000", value)  # unparsable
    assert not ledger_roles.same_account(None, value)
    assert not ledger_roles.same_account("", value)
