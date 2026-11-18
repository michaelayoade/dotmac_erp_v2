# ruff: noqa: F811  (imported fixtures are used by name)
"""Reviewed admission (B-1) in settled admission, against real PostgreSQL (hosted CI only).

The scene is the one of ``test_settled_admission_pg`` (legacy snapshot plus imported destination rows).
"""

import pytest
import sqlalchemy as sa
from dotmac_kernel.audit import AuditEvent
from sqlalchemy.orm import Session
from test_reviewed_admission_pg import record, row
from test_settled_admission_pg import (  # noqa: F401  (fixtures and helpers of the settled suite)
    SNAP,
    audit_registry,
    cli,
    counts,
    engine,
    setup,
)


def decide(engine, env, iid, *waived):
    return record(engine, env, [row(iid, waived=waived)], snapshot=SNAP)


def test_a_waived_check_admits_a_settled_invoice_and_the_audit_names_the_waiver(engine, setup):
    env, scene = setup
    iid = scene.add("AP-1", taxed="A3")  # legacy GL tax treatment not recognised
    scene.build()
    assert scene.run([iid], apply=False)["refusals_by_reason"] == {
        "legacy_evidence:legacy_gl_tax_treatment_unrecognised": 1
    }
    decide(engine, env, iid, "legacy_gl_tax_treatment_unrecognised")
    plan, applied = scene.plan_then_apply([iid])
    assert plan["counts"]["eligible"] == 1 and applied["applied"] == 1
    with Session(engine) as db:
        event = db.scalar(
            sa.select(AuditEvent).where(
                AuditEvent.tenant_id == env.tenant_id,
                AuditEvent.action == "erp.finance.supplier_invoice.legacy_settled_admitted",
                AuditEvent.entity_id == str(iid),
            )
        )
    assert event.details["waived_checks"] == ["legacy_gl_tax_treatment_unrecognised"]
    assert event.details["review_decision"] == "admit_with_waivers" and event.details["decision_file_sha"]


def test_a_non_waivable_check_still_refuses_a_settled_invoice_that_carries_a_waiver(engine, setup):
    env, scene = setup
    iid = scene.add("AP-1", taxed="A3")
    scene.build()
    scene.edit("journal_entry", "journal_entry_id", scene.journals[iid], status="DRAFT")  # the payment journal
    scene.build()
    decide(engine, env, iid, "legacy_gl_tax_treatment_unrecognised")
    plan = scene.run([iid], apply=False)
    assert plan["counts"]["eligible"] == 0 and plan["refusals_by_reason"] == {
        "legacy_evidence:payment_journal_not_posted": 1
    }
    before = counts(engine, env.tenant_id)
    with pytest.raises(cli.SettledRefusal, match="list_not_fully_eligible"):
        scene.run([iid], apply=True, digest=plan["plan_digest"])
    assert counts(engine, env.tenant_id) == before


def test_the_wrong_waiver_does_not_admit_and_a_stale_one_is_refused(engine, setup):
    env, scene = setup
    iid, clean = scene.add("AP-1", taxed="A3"), scene.add("AP-2")
    scene.build()
    decide(engine, env, iid, "journal_owner")  # not the check that fails
    decide(engine, env, clean, "line_totals")  # the invoice is fine: nothing to waive
    plan = scene.run([iid, clean], apply=False)
    assert plan["refusals_by_reason"] == {
        "legacy_evidence:legacy_gl_tax_treatment_unrecognised": 1,
        "legacy_evidence:waiver_not_needed": 1,
    }
