"""Q1 legacy VAT extraction (P-B16-e): the pure derivation, its tie, determinism, overrides and file modes."""

import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest

from erp_v2.billing_gl import vat_extract as vx
from erp_v2.billing_gl import vat_opening
from erp_v2.migration_ledger.common import LedgerRefused

D = Decimal
DEF, CUR, TAX = str(uuid4()), str(uuid4()), str(uuid4())
TENANT = uuid4()


def tax_map(**extra):
    return {TAX: vx.TaxInfo("VAT7.5", DEF, CUR), **extra}


def line(source, doc, credit, *, module="AR", currency="NGN", account=DEF):
    return vx.GlLine(account, module, source, doc, D(credit), currency)


def invoice(number="INV-1", total="120.00", tax="20.00"):
    return vx.Invoice(number, D(total), "NGN", [(TAX, D(tax))], [(TAX, D(tax))])


def carried(key, open_initial="120.00"):
    return vx.Carried(key, uuid4(), D(open_initial), "NGN")


def legacy(gl, invoices, allocations=()):
    return vx.Legacy(tax_map(), list(gl), dict(invoices), list(allocations))


def test_remaining_is_the_credit_less_legacys_own_prorate() -> None:
    inv = carried("i1", "60.00")
    data = legacy(
        [line("INVOICE", "i1", "20.00"), line(vx.RECLASS_SOURCE, "p1", "-10.00")],
        {"i1": invoice()},
        [("p1", "i1", D("60.00"))],
    )
    body, report = vx.derive(TENANT, [inv], data)
    item = body["items"][0]
    assert item["tax"] == "20.000000" and item["reclass"] == ["10.000000"] and item["open_initial"] == "60.000000"
    assert vat_opening.derive({**vat_opening.parse(json.dumps(body).encode())["items"][0]})[0] == D("10.00")
    assert report["items_loadable"] == 1 and report["tie"]["gl_net_credit"] == "10.000000"


def test_the_deferral_journal_and_restore_journal_count_as_invoice_credits_and_voids_net_out() -> None:
    inv = carried("i1")
    data = legacy(
        [
            line("INVOICE", "i1", "20.00"),
            line("INVOICE", "i1", "-20.00"),  # the invoice was voided (reversal keeps the source key) ...
            line("AR_INVOICE_RESTORE", "i1", "20.00"),  # ... and restored
        ],
        {"i1": invoice()},
    )
    body, report = vx.derive(TENANT, [inv], data)
    assert body["items"][0]["journal_1455_credit"] == "20.000000" and report["credit_mismatch_invoices"] == 0
    direct = legacy([line("AR_INVOICE_VAT_DEFERRAL", "i1", "20.00")], {"i1": invoice()})
    assert vx.derive(TENANT, [inv], direct)[0]["items"][0]["reclass"] == []


def test_a_bounced_payment_is_not_counted_and_the_legacy_credit_mismatch_is_surfaced() -> None:
    inv = carried("i1")
    bounced = legacy(
        [
            line("INVOICE", "i1", "20.00"),
            line(vx.RECLASS_SOURCE, "p1", "-10.00"),
            line(vx.RECLASS_SOURCE, "p1", "10.00"),
        ],
        {"i1": invoice()},
        [("p1", "i1", D("60.00"))],
    )
    body, report = vx.derive(TENANT, [inv], bounced)
    assert body["items"][0]["reclass"] == [] and report["payments_counted"] == 0
    wrong = legacy([line("INVOICE", "i1", "19.99")], {"i1": invoice()})  # posted credit != the lines' tax
    body, report = vx.derive(TENANT, [inv], wrong)
    assert report["credit_mismatch_invoices"] == 1 and body["items"][0]["reclass"] is None
    assert report["items_e38"] == 1


def test_a_payment_that_does_not_tie_to_its_journal_makes_its_invoices_unattributable() -> None:
    inv = carried("i1")
    data = legacy(
        [line("INVOICE", "i1", "20.00"), line(vx.RECLASS_SOURCE, "p1", "-9.99")],  # journal 9.99, prorate gives 10.00
        {"i1": invoice()},
        [("p1", "i1", D("60.00"))],
    )
    body, report = vx.derive(TENANT, [inv], data)
    assert body["items"][0]["reclass"] is None and report["payments_unattributable"] == 1
    assert report["tie"]["reclass_diff"] == "0.010000"  # the difference is reported, not hidden


def test_zero_tax_invoice_is_a_zero_position_and_every_carried_invoice_appears_once() -> None:
    zero = carried("z1", "50.00")
    two = carried("t1")
    legacy_two = vx.Invoice("INV-T", D("120.00"), "NGN", [(TAX, D("10.00")), (TAX, D("10.00"))], [(TAX, D("20.00"))])
    data = legacy(
        [line("INVOICE", "t1", "20.00")],
        {"z1": vx.Invoice("INV-Z", D("50.00"), "NGN", [(None, D(0))], []), "t1": legacy_two},
    )
    body, report = vx.derive(TENANT, [zero, two], data)
    by_doc = {}
    for item in body["items"]:
        by_doc.setdefault(item["document_id"], []).append(item)
    assert len(by_doc) == 2 and all(len(v) == 1 for v in by_doc.values())  # one treatment each, exactly once
    z = by_doc[str(zero.document_id)][0]
    assert z["treatment"] == "none" and z["tax"] == "0.000000" and z["deferral_account"] is None
    assert vat_opening.plan(vat_opening.parse(json.dumps(body).encode()), "a" * 64)["counts"]["load"] == 2
    assert report["zero_tax_items"] == 1


def test_refusals_ngn_only_missing_legacy_duplicates() -> None:
    inv = carried("i1")
    foreign = legacy([line("INVOICE", "i1", "20.00", currency="USD")], {"i1": invoice()})
    with pytest.raises(LedgerRefused) as non_ngn:
        vx.derive(TENANT, [inv], foreign)
    assert non_ngn.value.code == "non_ngn_unsupported"
    with pytest.raises(LedgerRefused) as missing:
        vx.derive(TENANT, [inv], legacy([], {}))
    assert missing.value.code == "carried_invoice_not_in_legacy"
    with pytest.raises(LedgerRefused) as dup:
        vx.derive(TENANT, [inv, inv], legacy([], {"i1": invoice()}))
    assert dup.value.code == "carried_invoice_duplicated"
    foreign_carried = vx.Carried("i1", uuid4(), D("1.00"), "USD")
    with pytest.raises(LedgerRefused):
        vx.derive(TENANT, [foreign_carried], legacy([], {"i1": invoice()}))


def test_the_tie_is_exact_and_names_what_no_invoice_or_payment_owns() -> None:
    inv = carried("i1", "60.00")
    data = legacy(
        [
            line("INVOICE", "i1", "20.00"),
            line(vx.RECLASS_SOURCE, "p1", "-10.00"),
            line("INVOICE", "other", "7.00"),  # a non-carried invoice's remainder
            line("RECLASS", None, "57.00", module="GL"),  # a manual journal on the deferral account
        ],
        {"i1": invoice(), "other": invoice("INV-O", "70.00", "7.00")},
        [("p1", "i1", D("60.00"))],
    )
    _, report = vx.derive(TENANT, [inv], data)
    tie = report["tie"]
    assert tie["gl_net_credit"] == "74.000000"
    assert (tie["carried_remaining"], tie["other_invoice_remaining"], tie["other_net"]) == (
        "10.000000",
        "7.000000",
        "57.000000",
    )
    assert tie["other_by_source"] == {"GL/RECLASS": "57.000000"}


def test_the_sealed_output_is_deterministic_and_input_order_independent() -> None:
    a, b = carried("a"), carried("b")
    gl = [line("INVOICE", "a", "20.00"), line("INVOICE", "b", "20.00")]
    inv = {"a": invoice("A"), "b": invoice("B")}
    first = vx.seal(vx.derive(TENANT, [a, b], legacy(gl, inv))[0])
    second = vx.seal(vx.derive(TENANT, [b, a], legacy(list(reversed(gl)), inv))[0])
    assert first == second and len(first[1]) == 64 and first[0].endswith(b"\n")


def test_finance_overrides_are_a_separate_named_input_bound_by_sha() -> None:
    inv = carried("i1")
    body, _ = vx.derive(TENANT, [inv], legacy([line("INVOICE", "i1", "19.99")], {"i1": invoice()}))  # E38
    item = body["items"][0]
    override = {
        "version": 1,
        "named_by": "finance.lead",
        "overrides": [
            {
                "document_id": item["document_id"],
                "treatment": item["treatment"],
                "deferred": "20.00",
                "evidence_sha256": "e" * 64,
                "ref": "FIN-9",
            }
        ],
    }
    raw = json.dumps(override).encode()
    merged, meta = vx.apply_overrides(body, raw)
    assert merged["items"][0]["finance_override"]["deferred"] == "20.000000" and meta["count"] == 1
    assert meta["named_by"] == "finance.lead" and len(meta["sha256"]) == 64
    assert vat_opening.plan(vat_opening.parse(json.dumps(merged).encode()), "a" * 64)["counts"]["override"] == 1
    override["overrides"][0]["document_id"] = str(uuid4())
    with pytest.raises(LedgerRefused) as unmatched:
        vx.apply_overrides(body, json.dumps(override).encode())
    assert unmatched.value.code == "override_unmatched"


def test_prorate_is_legacys_half_even_to_the_cent() -> None:
    assert vx.prorate(D("50.00"), D("0.01"), D("100.01")) == D("0.00")
    assert vx.prorate(D("1.00"), D("0.05"), D("2.00")) == D("0.02")  # 0.025 -> half even -> 0.02
    assert vx.prorate(D("1.00"), D("0.15"), D("2.00")) == D("0.08")  # 0.075 -> half even -> 0.08
    assert vx.prorate(D("1.00"), D("1.00"), D("0")) == D("0")


def test_output_files_are_created_exclusively_with_mode_0600(tmp_path) -> None:
    path = tmp_path / "items.json"
    fd = vx._create(path)
    os.close(fd)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(FileExistsError):
        vx._create(path)
