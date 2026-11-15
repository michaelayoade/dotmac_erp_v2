"""Enrolment minting: create the GENERATED rows the maps read.

Several module destinations are GENERATED in the wave pairings -
rows no legacy table copies into (v1 pay-structure revisions, tax
authorities and re-published rules, sourcing-event lines, sales
pipelines/leads, document libraries/types). The enrolment map
builders (enrolment.py) READ those rows; this module MINTS them
from legacy evidence under explicit rulings:

- Deterministic ids (uuid5 over step + tenant + natural key), so a
  rehearsal can be re-run and receipts compared byte-for-byte.
- Idempotent: a natural key already present in the destination is
  counted `existing`, never duplicated or updated.
- Caller owns the destination transaction (same contract as the
  executor); minting flushes inserts only.
- Anything needing a policy/config ruling the run does not supply
  is a named refusal or per-row blocker - never an invented value.
  Declared enrolment constants live on MintContext where the
  rehearsal can see and re-rule them.

Map-feeding mints run lazily from EnrolmentMaps when a spec first
needs the map (spec order guarantees parents are imported); the
rest run as an explicit phase via `mint_all`.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

import sqlalchemy as sa

_NS = uuid.NAMESPACE_URL


class MintRefusal(ValueError):
    """A mint step lacks a ruling/config value the run must supply."""


@dataclass(frozen=True)
class MintContext:
    tenant_id: Any  # run extent tenant (legacy rows without org columns)
    actor_id: str | None = None  # enrolment actor (published_by/created_by)
    occurred_at: datetime | None = None
    effective_from: date | None = None
    payroll_net_payable_account_ref: str | None = None
    # legacy tax_type -> ruled module code fields for rule re-publication
    tax_rule_policy: Mapping[str, Mapping[str, Any]] | None = None
    # legacy tax_band.name -> legacy tax_code string the bands belong to
    tax_band_rule_binding: Mapping[str, str] | None = None
    sales_pipeline_name: str = "Legacy import"
    sales_stage_name: str = "Imported"
    sales_lead_status: str = "new"  # LeadStatus.NEW, declared enrolment value
    document_library_code: str = "hr"
    document_library_name: str = "HR Documents"


@dataclass
class MintResult:
    step: str
    created: int = 0
    existing: int = 0
    blockers: list[str] = field(default_factory=list)
    refused: str | None = None
    excluded_sub_origin: int = 0  # Sub-tagged legacy rows deliberately not minted (M11 pending)
    details: dict[str, int] = field(default_factory=dict)  # named counts (never values)


def _norm(value: Any) -> Any:
    return value.hex if isinstance(value, uuid.UUID) else value


def _mint_id(step: str, tenant: Any, natural_key: str) -> str:
    return uuid.uuid5(_NS, f"erp-v2-enrolment:{step}:{_norm(tenant)}:{natural_key}").hex


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")[:80].rstrip("-")


def _insert(dest: sa.Connection, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    sql = sa.text(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(f':{c}' for c in columns)})"  # noqa: S608
    )
    dest.execute(sql, [dict(r) for r in rows])


def _now(ctx: MintContext) -> datetime:
    if ctx.occurred_at is None:
        raise MintRefusal("MintContext.occurred_at is required")
    return ctx.occurred_at


# --- payroll: one published v1 revision per imported structure -------------


def mint_pay_structure_revisions(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    if not ctx.payroll_net_payable_account_ref:
        raise MintRefusal(
            "pay_structure_revisions minting requires payroll_net_payable_account_ref "
            "(tenant payroll-payable account config; legacy structures carry none)"
        )
    if not ctx.actor_id:
        raise MintRefusal("pay_structure_revisions minting requires an enrolment actor_id (published_by_id)")
    result = MintResult("pay_structure_revisions")
    imported = {_norm(r[0]) for r in dest.execute(sa.text("SELECT id FROM mod_payroll.pay_structures"))}
    existing = {
        _norm(r[0])
        for r in dest.execute(sa.text("SELECT structure_id FROM mod_payroll.pay_structure_revisions WHERE version = 1"))
    }
    rows: list[dict[str, Any]] = []
    not_imported = 0
    legacy = source.execute(
        sa.text("SELECT structure_id, organization_id, created_at FROM salary_structure")
    ).mappings()
    for row in legacy:
        structure_id = _norm(row["structure_id"])
        if structure_id in existing:
            result.existing += 1
            continue
        if structure_id not in imported:
            not_imported += 1
            continue
        created = row["created_at"]
        effective = ctx.effective_from or (
            created.date() if isinstance(created, datetime) else date.fromisoformat(str(created)[:10])
        )
        rows.append(
            {
                "id": _mint_id("pay_structure_revisions", row["organization_id"], str(structure_id)),
                "tenant_id": _norm(row["organization_id"]),
                "structure_id": structure_id,
                "version": 1,
                "effective_from": effective,
                "effective_to": None,
                "net_payable_account_ref": ctx.payroll_net_payable_account_ref,
                "published_by_id": ctx.actor_id,
                "published_at": _now(ctx),
            }
        )
    if not_imported:
        result.blockers.append(f"{not_imported} legacy structures not yet imported (mint after pay_structures)")
    _insert(dest, "mod_payroll.pay_structure_revisions", rows)
    result.created = len(rows)
    return result


# --- tax: one authority per (tenant, legacy jurisdiction level) ------------


def mint_tax_authorities(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    result = MintResult("tax_authorities")
    existing = {
        (_norm(r[0]), r[1])
        for r in dest.execute(sa.text("SELECT tenant_id, authority_level_code FROM mod_tax.tax_authorities"))
    }
    names: dict[tuple[Any, str], set[str]] = {}
    for tenant, level, name in source.execute(
        sa.text(
            "SELECT DISTINCT organization_id, jurisdiction_level, tax_authority_name "
            "FROM tax_jurisdiction WHERE jurisdiction_level IS NOT NULL"
        )
    ):
        names.setdefault((_norm(tenant), str(level)), set()).add(str(name or ""))
    rows: list[dict[str, Any]] = []
    for (tenant, level), level_names in sorted(names.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if (tenant, level) in existing:
            result.existing += 1
            continue
        distinct = {n for n in level_names if n}
        if len(distinct) > 1:
            raise MintRefusal(
                f"tax authority level {level!r} has multiple legacy authority names {sorted(distinct)}; "
                "rehearsal must rule the canonical authority name"
            )
        rows.append(
            {
                "id": _mint_id("tax_authorities", tenant, level),
                "tenant_id": tenant,
                "code": level,
                "name": next(iter(distinct), level),
                "authority_level_code": level,
                "status": "active",
                "created_at": _now(ctx),
                "updated_at": _now(ctx),
            }
        )
    _insert(dest, "mod_tax.tax_authorities", rows)
    result.created = len(rows)
    return result


# --- tax: rule re-publication (policy is re-issued, never row-copied) ------

_TAX_RULE_POLICY_FIELDS: Final = (
    "fact_kind",
    "recognition_basis_code",
    "transaction_side",
    "calculation_method",
    "treatment_code",
    "calculation_base_code",
)


def mint_tax_rules(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    result = MintResult("tax_rules")
    legacy_codes = list(
        source.execute(
            sa.text(
                "SELECT tax_code_id, organization_id, tax_code, tax_type, tax_rate, "
                "effective_from, effective_to, is_inclusive, recovery_rate, is_recoverable "
                "FROM tax_code WHERE is_active"
            )
        ).mappings()
    )
    if not ctx.tax_rule_policy:
        types = sorted({str(r["tax_type"]) for r in legacy_codes})
        raise MintRefusal(
            "tax rule re-publication requires MintContext.tax_rule_policy mapping each "
            f"legacy tax_type to ruled module codes {_TAX_RULE_POLICY_FIELDS}; legacy types present: {types}"
        )
    existing = {
        _norm(r[0]) for r in dest.execute(sa.text("SELECT tax_code_id FROM mod_tax.tax_rules WHERE version = 1"))
    }
    rule_by_code: dict[str, tuple[Any, str]] = {}  # legacy tax_code string -> (tenant, rule id)
    rows: list[dict[str, Any]] = []
    sequence: dict[Any, int] = {}
    for row in sorted(legacy_codes, key=lambda r: (str(_norm(r["organization_id"])), str(r["tax_code"]))):
        tenant = _norm(row["organization_id"])
        rule_id = _mint_id("tax_rules", tenant, str(_norm(row["tax_code_id"])))
        rule_by_code[str(row["tax_code"])] = (tenant, rule_id)
        if _norm(row["tax_code_id"]) in existing:
            result.existing += 1
            continue
        policy = ctx.tax_rule_policy.get(str(row["tax_type"]))
        if policy is None or any(f not in policy for f in _TAX_RULE_POLICY_FIELDS):
            result.blockers.append(
                f"tax_code {row['tax_code']!r}: no complete policy ruling for type {row['tax_type']!r}"
            )
            continue
        sequence[tenant] = sequence.get(tenant, 0) + 1
        effective_from = row["effective_from"] or ctx.effective_from
        if effective_from is None:
            result.blockers.append(f"tax_code {row['tax_code']!r}: no effective_from (legacy null, no ctx date)")
            continue
        rows.append(
            {
                "id": rule_id,
                "tenant_id": tenant,
                # mod_tax publication lifecycle (tx_0004): inserts
                # must begin unpublished; bands attach; then a
                # publish-only UPDATE flips is_published (deferred
                # trigger requires published by commit).
                "is_published": False,
                "tax_code_id": _norm(row["tax_code_id"]),
                "version": 1,
                "effective_from": effective_from,
                "effective_to": row["effective_to"],
                "priority": int(policy.get("priority", sequence[tenant])),
                "rate": row["tax_rate"],
                "fixed_amount": None,
                "inclusive": bool(row["is_inclusive"]),
                "recoverable_rate": row["recovery_rate"] if row["is_recoverable"] else 0,
                "calculation_sequence": sequence[tenant],
                "published_at": _now(ctx),
                **{f: policy[f] for f in _TAX_RULE_POLICY_FIELDS},
            }
        )
    _insert(dest, "mod_tax.tax_rules", rows)
    result.created += len(rows)

    # Bands attach only under an explicit name -> tax_code binding.
    # Legacy deployments without a tax_band table have no bands.
    # No savepoint probe on Postgres: the shared source connection
    # runs in psycopg server-cursor mode after executor streaming,
    # where SAVEPOINT itself is invalid (DECLARE CURSOR FOR
    # SAVEPOINT - found live at the Gate 5 re-rehearsal). The
    # to_regclass existence check resolves through search_path
    # and never errors; other dialects keep the savepoint probe.
    band_sql = sa.text(
        "SELECT tax_band_id, organization_id, name, min_amount, max_amount, rate, sequence "
        "FROM tax_band WHERE is_active"
    )
    if source.dialect.name == "postgresql":
        present = bool(source.execute(sa.text("SELECT to_regclass('tax_band') IS NOT NULL")).scalar_one())
        bands = list(source.execute(band_sql).mappings()) if present else []
    else:
        try:
            with source.begin_nested():
                bands = list(source.execute(band_sql).mappings())
        except sa.exc.SQLAlchemyError:
            bands = []
    if bands and not ctx.tax_band_rule_binding:
        unbound = sorted({str(b["name"]) for b in bands})
        result.blockers.append(f"tax bands not minted: no tax_band_rule_binding for band names {unbound}")
        bands = []  # rules still publish below (deferred trigger)
    existing_bands = {
        (_norm(r[0]), r[1]) for r in dest.execute(sa.text("SELECT rule_id, sequence FROM mod_tax.tax_rule_bands"))
    }
    band_rows: list[dict[str, Any]] = []
    for band in bands:
        binding = (ctx.tax_band_rule_binding or {}).get(str(band["name"]))
        target = rule_by_code.get(binding or "")
        if target is None:
            result.blockers.append(f"tax band {band['name']!r}: binding {binding!r} names no minted rule")
            continue
        tenant, rule_id = target
        if (rule_id, band["sequence"]) in existing_bands:
            result.existing += 1
            continue
        band_rows.append(
            {
                "id": _mint_id("tax_rule_bands", tenant, str(_norm(band["tax_band_id"]))),
                "tenant_id": tenant,
                "rule_id": rule_id,
                "sequence": band["sequence"],
                "lower_bound": band["min_amount"],
                "upper_bound": band["max_amount"],
                "rate": band["rate"],
            }
        )
    _insert(dest, "mod_tax.tax_rule_bands", band_rows)
    result.created += len(band_rows)
    minted_rule_ids = [r["id"] for r in rows]
    for start in range(0, len(minted_rule_ids), 10000):
        dest.execute(
            sa.text(
                "UPDATE mod_tax.tax_rules SET is_published = true WHERE id IN :ids AND NOT is_published"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": minted_rule_ids[start : start + 10000]},
        )
    return result


# --- procurement: sourcing lines snapshot requisition lines per RFQ -------


def mint_sourcing_event_lines(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    if ctx.tenant_id is None:
        raise MintRefusal("sourcing_event_lines minting requires MintContext.tenant_id (legacy RFQ lines carry no org)")
    result = MintResult("sourcing_event_lines")
    tenant = _norm(ctx.tenant_id)
    lines_by_requisition: dict[Any, list[Mapping[str, Any]]] = {}
    for line in source.execute(
        sa.text(
            "SELECT line_id, requisition_id, line_number, item_id, description, quantity, "
            "uom, estimated_unit_price, delivery_date FROM purchase_requisition_line"
        )
    ).mappings():
        lines_by_requisition.setdefault(_norm(line["requisition_id"]), []).append(line)
    rfqs = list(
        source.execute(
            sa.text("SELECT rfq_id, requisition_id FROM request_for_quotation WHERE requisition_id IS NOT NULL")
        ).mappings()
    )
    shared = [req for req, n in _counts(rfq["requisition_id"] for rfq in rfqs).items() if n > 1]
    if shared:
        result.blockers.append(
            f"{len(shared)} requisitions feed multiple RFQs; their bid lines will conflict in the "
            "sourcing_line map until rehearsal rules the owning event"
        )
    existing = {
        (_norm(r[0]), _norm(r[1]))
        for r in dest.execute(
            sa.text(
                "SELECT event_id, source_requisition_line_id FROM mod_procurement.sourcing_event_lines "
                "WHERE source_requisition_line_id IS NOT NULL"
            )
        )
    }
    rows: list[dict[str, Any]] = []
    for rfq in rfqs:
        event_id = _norm(rfq["rfq_id"])
        for line in lines_by_requisition.get(_norm(rfq["requisition_id"]), []):
            line_id = _norm(line["line_id"])
            if (event_id, line_id) in existing:
                result.existing += 1
                continue
            rows.append(
                {
                    "id": _mint_id("sourcing_event_lines", tenant, f"{event_id}:{line_id}"),
                    "tenant_id": tenant,
                    "event_id": event_id,
                    "line_number": line["line_number"],
                    "description": line["description"],
                    "quantity": line["quantity"],
                    "unit": line["uom"],
                    "source_requisition_line_id": line_id,
                    "item_ref": str(_norm(line["item_id"])) if line["item_id"] is not None else None,
                    "target_unit_cost": line["estimated_unit_price"],
                    "requested_delivery_date": line["delivery_date"],
                    "created_at": _now(ctx),
                    "updated_at": _now(ctx),
                }
            )
    _insert(dest, "mod_procurement.sourcing_event_lines", rows)
    result.created = len(rows)
    return result


def _counts(values: Any) -> dict[Any, int]:
    out: dict[Any, int] = {}
    for value in values:
        key = _norm(value)
        out[key] = out.get(key, 0) + 1
    return out


# --- sales: enrolment pipeline/stage + one lead per legacy customer -------


def mint_sales_leads(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    result = MintResult("sales_leads")
    customers = list(
        source.execute(
            # F1: Sub-tagged customers are not leads (M11 pending)
            sa.text(
                "SELECT customer_id, organization_id, legal_name, trading_name, currency_code "
                "FROM customer WHERE dotmac_sub_id IS NULL"
            )
        ).mappings()
    )
    result.excluded_sub_origin = (
        source.execute(sa.text("SELECT count(*) FROM customer WHERE dotmac_sub_id IS NOT NULL")).scalar() or 0
    )
    tenants = sorted({_norm(c["organization_id"]) for c in customers}, key=str)
    pipeline_by_tenant: dict[Any, str] = {}
    stage_by_tenant: dict[Any, str] = {}
    existing_pipelines = {
        (_norm(r[0]), r[1]): _norm(r[2])
        for r in dest.execute(sa.text("SELECT tenant_id, name, id FROM mod_sales.pipelines"))
    }
    existing_stages = {
        (_norm(r[0]), r[1]): _norm(r[2])
        for r in dest.execute(sa.text("SELECT pipeline_id, name, id FROM mod_sales.pipeline_stages"))
    }
    pipeline_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    for tenant in tenants:
        pipeline_id = existing_pipelines.get((tenant, ctx.sales_pipeline_name)) or _mint_id(
            "sales_pipelines", tenant, ctx.sales_pipeline_name
        )
        if (tenant, ctx.sales_pipeline_name) not in existing_pipelines:
            pipeline_rows.append(
                {
                    "id": pipeline_id,
                    "tenant_id": tenant,
                    "name": ctx.sales_pipeline_name,
                    "description": "Enrolment pipeline for legacy-imported customers",
                    "is_active": True,
                    "created_at": _now(ctx),
                    "updated_at": _now(ctx),
                }
            )
            result.created += 1
        else:
            result.existing += 1
        pipeline_by_tenant[tenant] = pipeline_id
        stage_id = existing_stages.get((pipeline_id, ctx.sales_stage_name)) or _mint_id(
            "sales_pipeline_stages", tenant, f"{pipeline_id}:{ctx.sales_stage_name}"
        )
        if (pipeline_id, ctx.sales_stage_name) not in existing_stages:
            stage_rows.append(
                {
                    "id": stage_id,
                    "tenant_id": tenant,
                    "pipeline_id": pipeline_id,
                    "name": ctx.sales_stage_name,
                    "order_index": 1,
                    "default_probability": 0,
                    "is_active": True,
                    "created_at": _now(ctx),
                    "updated_at": _now(ctx),
                }
            )
            result.created += 1
        else:
            result.existing += 1
        stage_by_tenant[tenant] = stage_id
    _insert(dest, "mod_sales.pipelines", pipeline_rows)
    _insert(dest, "mod_sales.pipeline_stages", stage_rows)

    existing_leads = {
        (_norm(r[0]), r[1]) for r in dest.execute(sa.text("SELECT tenant_id, subject_opaque_id FROM mod_sales.leads"))
    }
    lead_rows: list[dict[str, Any]] = []
    for customer in customers:
        tenant = _norm(customer["organization_id"])
        opaque = str(_norm(customer["customer_id"]))
        if (tenant, opaque) in existing_leads:
            result.existing += 1
            continue
        if not customer["currency_code"]:
            result.blockers.append(f"customer {opaque}: no currency_code for lead minting")
            continue
        label = customer["trading_name"] or customer["legal_name"] or opaque
        lead_rows.append(
            {
                "id": _mint_id("sales_leads", tenant, opaque),
                "tenant_id": tenant,
                "subject_kind": "customer",
                "subject_opaque_id": opaque,
                "subject_version": None,
                "subject_label": label,
                "title": label,
                "pipeline_id": pipeline_by_tenant[tenant],
                "stage_id": stage_by_tenant[tenant],
                "status": ctx.sales_lead_status,
                "probability": 0,
                "currency": customer["currency_code"],
                "estimated_value": None,
                "expected_close_at": None,
                "notes": "Minted at ERP enrolment from the legacy customer estate",
                "won_at": None,
                "lost_at": None,
                "created_at": _now(ctx),
                "updated_at": _now(ctx),
            }
        )
    _insert(dest, "mod_sales.leads", lead_rows)
    result.created += len(lead_rows)
    return result


# --- documents: enrolment library + one type version per legacy category --


def mint_document_libraries_types(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    if not ctx.actor_id:
        raise MintRefusal("document library/type minting requires an enrolment actor_id (created_by)")
    result = MintResult("document_libraries_types")
    tenants_categories: dict[Any, set[str]] = {}
    uncategorised = 0
    for tenant, category in source.execute(sa.text("SELECT DISTINCT organization_id, category FROM hr_document")):
        slug = _slug(category)
        if not slug:
            uncategorised += 1
            continue
        tenants_categories.setdefault(_norm(tenant), set()).add(slug)
    if uncategorised:
        result.blockers.append(f"{uncategorised} (tenant, category) groups have no usable category for a type code")
    existing_libraries = {
        (_norm(r[0]), r[1])
        for r in dest.execute(sa.text("SELECT tenant_id, code FROM mod_documents.document_libraries"))
    }
    existing_types = {
        (_norm(r[0]), r[1], r[2])
        for r in dest.execute(sa.text("SELECT tenant_id, type_code, version FROM mod_documents.document_type_versions"))
    }
    library_rows: list[dict[str, Any]] = []
    type_rows: list[dict[str, Any]] = []
    for tenant, categories in sorted(tenants_categories.items(), key=lambda kv: str(kv[0])):
        if (tenant, ctx.document_library_code) in existing_libraries:
            result.existing += 1
        else:
            library_rows.append(
                {
                    "id": _mint_id("document_libraries", tenant, ctx.document_library_code),
                    "tenant_id": tenant,
                    "code": ctx.document_library_code,
                    "name": ctx.document_library_name,
                    "description": "Enrolment library for legacy HR documents",
                    "created_by": ctx.actor_id,
                    "created_at": _now(ctx),
                }
            )
        for slug in sorted(categories):
            if (tenant, slug, 1) in existing_types:
                result.existing += 1
                continue
            type_rows.append(
                {
                    "id": _mint_id("document_type_versions", tenant, f"{slug}:1"),
                    "tenant_id": tenant,
                    "type_code": slug,
                    "version": 1,
                    # Declared enrolment config: unconstrained v1 types;
                    # rehearsal tightens schemas/transitions per type.
                    "metadata_schema": "{}",
                    "required_fields": "[]",
                    "allowed_transitions": "[]",
                    "approval_required_states": "[]",
                    "major_minor": False,
                    "created_by": ctx.actor_id,
                    "created_at": _now(ctx),
                }
            )
    _insert(dest, "mod_documents.document_libraries", library_rows)
    _insert(dest, "mod_documents.document_type_versions", type_rows)
    result.created = len(library_rows) + len(type_rows)
    return result


def mint_pay_rule_bases(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Basis rows for converted percentage rules (runs post-specs).

    Rule ids are the preserved legacy earning/deduction ids; basis
    components resolve from formula abbreviations through the
    legacy salary_component estate of the same tenant.
    """

    from erp_v2.importer.payroll_formula import convert_rule

    result = MintResult("pay_rule_bases")
    components = list(
        source.execute(
            sa.text("SELECT component_id, organization_id, abbr FROM salary_component WHERE abbr IS NOT NULL")
        ).mappings()
    )
    abbr_component = {(_norm(c["organization_id"]), str(c["abbr"])): _norm(c["component_id"]) for c in components}
    known_abbrs = {str(c["abbr"]) for c in components}
    existing = {
        (_norm(r[0]), _norm(r[1]))
        for r in dest.execute(sa.text("SELECT rule_id, component_id FROM mod_payroll.pay_rule_bases"))
    }
    tenant_by_structure = {
        _norm(r[0]): _norm(r[1])
        for r in source.execute(sa.text("SELECT structure_id, organization_id FROM salary_structure"))
    }
    rows: list[dict[str, Any]] = []
    for table, pk in (("salary_structure_earning", "earning_id"), ("salary_structure_deduction", "deduction_id")):
        legacy = source.execute(
            sa.text(
                f"SELECT {pk} AS rule_id, structure_id, amount, amount_based_on_formula, formula, condition "  # noqa: S608
                f"FROM {table}"
            )
        ).mappings()
        for row in legacy:
            conversion = convert_rule(
                amount=row["amount"],
                amount_based_on_formula=row["amount_based_on_formula"],
                formula=row["formula"],
                condition=row["condition"],
                known_abbrs=known_abbrs,
            )
            if conversion is None or conversion.method != "percentage":
                continue
            tenant = tenant_by_structure.get(_norm(row["structure_id"]))
            rule_id = _norm(row["rule_id"])
            for abbr in conversion.basis_abbrs:
                component_id = abbr_component.get((tenant, abbr))
                if component_id is None:
                    result.blockers.append(f"rule {rule_id}: basis abbreviation {abbr!r} has no component")
                    continue
                if (rule_id, component_id) in existing:
                    result.existing += 1
                    continue
                rows.append(
                    {
                        "id": _mint_id("pay_rule_bases", tenant, f"{rule_id}:{component_id}"),
                        "tenant_id": tenant,
                        "rule_id": rule_id,
                        "component_id": component_id,
                    }
                )
    _insert(dest, "mod_payroll.pay_rule_bases", rows)
    result.created = len(rows)
    return result


def mint_kernel_tenants(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Kernel tenants from legacy organisations (UUID-preserving).

    Runs before specs: every tenant-scoped destination FKs to
    tenants.id. Slug from organization_code; duplicate slugs error
    (recorded refused) rather than silently renamed.
    """

    result = MintResult("kernel_tenants")
    existing = {_norm(r[0]) for r in dest.execute(sa.text("SELECT id FROM tenants"))}
    rows: list[dict[str, Any]] = []
    legacy = source.execute(
        sa.text("SELECT organization_id, organization_code, legal_name, trading_name, is_active FROM organization")
    ).mappings()
    for row in legacy:
        tenant_id = _norm(row["organization_id"])
        if tenant_id in existing:
            result.existing += 1
            continue
        slug = _slug(row["organization_code"] or row["legal_name"])
        if not slug:
            result.blockers.append(f"organisation {tenant_id}: no usable slug from code/name")
            continue
        rows.append(
            {
                "id": tenant_id,
                "slug": slug,
                "name": row["trading_name"] or row["legal_name"] or slug,
                "is_active": bool(row["is_active"]),
                "created_at": _now(ctx),
                "updated_at": _now(ctx),
            }
        )
    _insert(dest, "tenants", rows)
    result.created = len(rows)
    return result


def mint_bank_institutions_from_accounts(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Enrol institutions the tenant banks with but the directory lacks.

    Legacy bank_accounts name institutions absent from the imported
    CBN directory (fintechs: Paystack, Flutterwave, TAJ). One
    directory row is minted per unmatched legacy bank name, with a
    declared directory-local code (slug of the name) - the legacy
    bank_code on those rows is placeholder text ('0000', account
    numbers), never a sort code, so it is not reused as one.
    Accounts whose bank_code or name already matches an imported
    institution mint nothing.
    """

    result = MintResult("bank_institutions_from_accounts")
    existing_codes: dict[str, str] = {}
    existing_names: dict[str, str] = {}
    for code, name, _inst_id in dest.execute(
        sa.text("SELECT code, name, id FROM mod_banking.bank_institutions WHERE tenant_id = :t"),
        {"t": ctx.tenant_id},
    ):
        existing_codes[str(code)] = str(name)
        existing_names[str(name).casefold()] = str(code)
    rows: list[dict[str, Any]] = []
    seen_names: dict[str, str] = {}
    legacy = source.execute(sa.text("SELECT DISTINCT bank_name, bank_code FROM bank_accounts")).mappings()
    for row in legacy:
        name = str(row["bank_name"] or "").strip()
        code = str(row["bank_code"] or "").strip()
        if not name:
            continue
        if code and code in existing_codes:
            continue
        if name.casefold() in existing_names:
            continue
        if name.casefold() in seen_names:
            continue
        mint_code = _slug(name).upper().replace("-", "_")[:60]
        if mint_code in existing_codes or mint_code in seen_names.values():
            result.blockers.append(
                f"bank institution {name!r}: minted code {mint_code!r} collides with an existing directory code"
            )
            continue
        seen_names[name.casefold()] = mint_code
        rows.append(
            {
                "id": _mint_id("bank_institutions_from_accounts", ctx.tenant_id, mint_code),
                "tenant_id": ctx.tenant_id,
                "code": mint_code,
                "name": name,
                "country_code": "NG",
                "status": "active",
                "created_at": _now(ctx),
                "updated_at": _now(ctx),
            }
        )
    _insert(dest, "mod_banking.bank_institutions", rows)
    result.created = len(rows)
    return result


# --- payees: legacy bank details migrate (complete: approved; incomplete: proposed) (B8) ---


def _canon_ref(value: Any) -> str:
    """Canonical dashed lowercase UUID spelling when the value is a UUID, else the text."""

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _supplier_bank_details(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _migration_actor(actor_id: str) -> uuid.UUID:
    """The declared enrolment actor as a party-shaped id: itself if a UUID, else a stable uuid5."""

    try:
        return uuid.UUID(str(actor_id))
    except ValueError:
        return uuid.uuid5(_NS, f"erp-v2-migration-actor:{actor_id}")


def _write_migration_audit(dest: sa.Connection, ctx: MintContext, approved: Sequence[Mapping[str, Any]]) -> None:
    """One audit event per migrated approval, in the caller's transaction (fail closed)."""

    from dotmac_kernel.audit import write_audit_event
    from dotmac_kernel.audit_actions import AuditActionRegistry, AuditActionsNotInstalledError, install_audit_actions
    from dotmac_kernel.features import FeatureManifest
    from sqlalchemy.orm import Session

    from erp_v2.payees.service import AUDIT_ACTIONS, AUDIT_MIGRATED, SOURCE_APPLICATION

    try:
        from dotmac_kernel.audit_actions import active_audit_actions

        active_audit_actions()
    except AuditActionsNotInstalledError:
        # Importer CLI builds no app: declare the payee vocabulary itself, as the kernel requires.
        install_audit_actions(
            AuditActionRegistry.from_manifests([FeatureManifest(name="erp_payee_bank", audit_actions=AUDIT_ACTIONS)])
        )
    with Session(bind=dest, join_transaction_mode="create_savepoint") as session:
        for row in approved:
            write_audit_event(
                session,
                tenant_id=uuid.UUID(str(ctx.tenant_id)),
                actor_type="service",
                actor_id=str(ctx.actor_id),
                actor_label="erp_v2.importer",
                source_application=SOURCE_APPLICATION,
                action=AUDIT_MIGRATED,
                entity_type="payee_bank_instruction",
                entity_id=str(uuid.UUID(str(row["id"]))),
                is_success=True,
                details={
                    "payee_kind": row["payee_kind"],
                    "payee_ref": row["payee_ref"],
                    "account_last4": row["account_last4"],
                    "source_ref": row["source_ref"],
                    "source_fingerprint": row["source_fingerprint"],
                },
            )
        session.commit()  # releases only the session's savepoint; the caller owns the real transaction


def mint_payee_bank_instructions(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Migrate retained legacy payee bank details (Michael, register v1294: data migration).

    Sources: ``employee.bank_name/bank_account_number/bank_account_name`` and
    ``supplier.bank_details`` (JSON).

    - COMPLETE rows (bank code 3-6 digits, 10-digit account number, account name) land directly as
      ``approved``, origin ``legacy_import``, decided by the declared enrolment actor, with
      provenance (``source_ref``), a sha256 source fingerprint and one audit event each
      (``erp.payees.bank_details.migrated``). No human proposer or approver is needed.
    - INCOMPLETE rows (e.g. every legacy employee row: it carries a branch code, never a bank code)
      land as ``proposed``/``legacy_import`` and are never active: Finance corrects them through
      the normal two-person propose/approve (a human proposal replaces the open legacy row).
    - Every change after migration stays two-person.
    - Employees whose destination row was not imported are not minted (blocker by id).
    - Rows with no full account number (empty, or supplier last-4 only) mint nothing.
    - Blockers, details and receipts name legacy ids and counts only, never numbers or names.
    - A payee that already has a proposed or approved instruction is counted ``existing``.
    """

    result = MintResult("payee_bank_instructions")
    if not ctx.actor_id:
        raise MintRefusal("payee_bank_instructions minting requires an enrolment actor_id (migration actor)")
    from erp_v2.payees.service import missing_fields

    tenant = _canon_ref(ctx.tenant_id)
    stamp = _now(ctx)
    actor_party = _migration_actor(ctx.actor_id)
    imported_employees = {
        _canon_ref(r[0])
        for r in dest.execute(sa.text("SELECT id FROM mod_people.employees WHERE tenant_id = :t"), {"t": ctx.tenant_id})
    }
    live = {
        (str(kind), str(ref))
        for kind, ref in dest.execute(
            sa.text(
                "SELECT payee_kind, payee_ref FROM payee_bank_instructions "
                "WHERE tenant_id = :t AND status IN ('proposed', 'approved')"
            ),
            {"t": ctx.tenant_id},
        )
    }
    candidates: list[dict[str, Any]] = []
    not_imported = 0

    def _add(kind: str, ref: str, label: str, fields: dict[str, Any], source_ref: str) -> None:
        number = str(fields.get("account_number") or "").strip()
        if not number:
            return
        values = {
            "bank_code": (str(fields.get("bank_code") or "").strip() or None),
            "bank_name": (str(fields.get("bank_name") or "").strip() or None),
            "account_name": (str(fields.get("account_name") or "").strip() or None),
        }
        too_long = (
            len(number) > 34
            or len(ref) > 255
            or len(label) > 200
            or len(values["bank_code"] or "") > 10
            or len(values["bank_name"] or "") > 120
            or len(values["account_name"] or "") > 200
        )
        if too_long:
            result.blockers.append(f"payee_bank_instructions: {kind} {ref} has an over-long legacy bank field")
            return
        if (kind, ref) in live:
            result.existing += 1
            return
        complete = not missing_fields(values["bank_code"], number, values["account_name"])
        fingerprint = hashlib.sha256(
            json.dumps(
                [_canon_ref(ctx.tenant_id), kind, ref, values["bank_code"], number, values["account_name"]],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        candidates.append(
            {
                "id": _mint_id("payee_bank_instructions", ctx.tenant_id, f"{kind}:{ref}"),
                "tenant_id": ctx.tenant_id,
                "payee_kind": kind,
                "payee_ref": ref,
                "payee_label": label or ref,
                **values,
                "account_number": number,
                "account_last4": number[-4:],
                "status": "approved" if complete else "proposed",
                "origin": "legacy_import",
                "evidence_ref": f"legacy-import:{source_ref}",
                "source_ref": f"legacy:{source_ref}:{ref}",
                "source_fingerprint": fingerprint,
                "proposed_by_party_id": None,
                "proposed_at": stamp,
                "decided_by_party_id": actor_party.hex if complete else None,
                "decided_at": stamp if complete else None,
                "approved_at": stamp if complete else None,
                "decision_note": f"legacy data migration by {ctx.actor_id}"[:500] if complete else None,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )

    for row in source.execute(
        sa.text(
            "SELECT employee_id, organization_id, employee_code, bank_name, bank_account_number, "
            "bank_account_name FROM employee"
        )
    ).mappings():
        if _canon_ref(row["organization_id"]) != tenant:
            continue
        ref = _canon_ref(row["employee_id"])
        if not str(row["bank_account_number"] or "").strip():
            continue
        if ref not in imported_employees:
            not_imported += 1
            continue
        _add(
            "employee",
            ref,
            str(row["employee_code"] or ""),
            {
                "bank_name": row["bank_name"],
                "account_number": row["bank_account_number"],
                "account_name": row["bank_account_name"],
            },
            "employee.bank_account",
        )
    for row in source.execute(
        sa.text("SELECT supplier_id, organization_id, legal_name, trading_name, bank_details FROM supplier")
    ).mappings():
        if _canon_ref(row["organization_id"]) != tenant:
            continue
        details = _supplier_bank_details(row["bank_details"])
        if not details:
            continue
        label = str(row["legal_name"] or "").strip() or str(row["trading_name"] or "").strip()
        _add("supplier", _canon_ref(row["supplier_id"]), label, details, "supplier.bank_details")
    if not_imported:
        result.blockers.append(f"payee_bank_instructions: {not_imported} employees with bank details not yet imported")
    _insert(dest, "payee_bank_instructions", candidates)
    approved = [c for c in candidates if c["status"] == "approved"]
    if approved:
        try:
            _write_migration_audit(dest, ctx, approved)
        except (ValueError, TypeError) as exc:  # fail closed: no approved row without its audit event
            raise MintRefusal(f"migration audit could not be written ({type(exc).__name__})") from exc
    result.created = len(candidates)
    result.details = {"approved": len(approved), "proposed_incomplete": len(candidates) - len(approved)}
    return result


def mint_functional_currency_settings(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Tenant functional currency setting from legacy ``organization.functional_currency_code``.

    One tenant-scoped ``accounting_policy/functional_currency`` row per organisation, with
    a history row naming the import. Never overwrites: an existing row with the same value
    counts ``existing``; a different value, an invalid code or a missing tenant is a named
    blocker, not a repair. Runs after ``kernel_tenants`` (FK to tenants).
    """

    result = MintResult("functional_currency_settings")
    tenants = {_norm(r[0]) for r in dest.execute(sa.text("SELECT id FROM tenants"))}
    existing = {
        _norm(r[0]): r[1]
        for r in dest.execute(
            sa.text(
                "SELECT tenant_id, value_text FROM domain_settings WHERE scope_kind = 'tenant' "
                "AND domain = :domain AND key = :key"
            ),
            {"domain": "accounting_policy", "key": "functional_currency"},
        )
    }
    rows: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    legacy = source.execute(sa.text("SELECT organization_id, functional_currency_code FROM organization")).mappings()
    for row in legacy:
        tenant_id = _norm(row["organization_id"])
        code = row["functional_currency_code"]
        code = code.strip().upper() if isinstance(code, str) else ""
        if len(code) != 3 or not code.isascii() or not code.isalpha():
            result.blockers.append(f"organisation {tenant_id}: functional currency is missing or not a 3-letter code")
            continue
        if tenant_id not in tenants:
            result.blockers.append(f"organisation {tenant_id}: tenant not minted yet")
            continue
        if tenant_id in existing:
            if existing[tenant_id] == code:
                result.existing += 1
            else:
                result.blockers.append(
                    f"organisation {tenant_id}: a different functional currency setting exists (not overwritten)"
                )
            continue
        setting_id = _mint_id("functional_currency_settings", tenant_id, "accounting_policy/functional_currency")
        rows.append(
            {
                "id": setting_id,
                "tenant_id": tenant_id,
                "scope_kind": "tenant",
                "domain": "accounting_policy",
                "key": "functional_currency",
                "value_type": "string",
                "value_text": code,
                "is_secret": False,
                "is_active": True,
                "created_at": _now(ctx),
                "updated_at": _now(ctx),
            }
        )
        history.append(
            {
                "id": _mint_id("functional_currency_settings_history", tenant_id, "create"),
                "tenant_id": tenant_id,
                "domain": "accounting_policy",
                "key": "functional_currency",
                "setting_id": setting_id,
                "action": "create",
                "value_after": code,
                "secret_changed": False,
                "changed_at": _now(ctx),
                "change_reason": "legacy organization.functional_currency_code import",
            }
        )
    _insert(dest, "domain_settings", rows)
    _insert(dest, "domain_setting_history", history)
    result.created = len(rows)
    return result


MINT_STEPS: Final[tuple[tuple[str, Any], ...]] = (
    ("kernel_tenants", mint_kernel_tenants),
    ("functional_currency_settings", mint_functional_currency_settings),
    ("pay_structure_revisions", mint_pay_structure_revisions),
    ("pay_rule_bases", mint_pay_rule_bases),
    ("tax_authorities", mint_tax_authorities),
    ("tax_rules", mint_tax_rules),
    ("sourcing_event_lines", mint_sourcing_event_lines),
    ("sales_leads", mint_sales_leads),
    ("document_libraries_types", mint_document_libraries_types),
    ("bank_institutions_from_accounts", mint_bank_institutions_from_accounts),
    ("payee_bank_instructions", mint_payee_bank_instructions),
)

# Enrolment map name -> mint step that must run before the map builds.
MAP_MINTS: Final[Mapping[str, str]] = {
    "structure_revision": "pay_structure_revisions",
    "tax_authority_by_level": "tax_authorities",
    "sourcing_line_by_requisition_line": "sourcing_event_lines",
    # Source-driven mints whose rows other specs' maps read: they must
    # run lazily BEFORE the consuming spec, not as post-specs steps
    # (Gate 4 dry-run: quote_lead built 0 leads because sales_leads
    # minted after the quotes spec had already run).
    "quote_lead": "sales_leads",
    "document_library": "document_libraries_types",
    "bank_institution_by_account": "bank_institutions_from_accounts",
}


def run_mint_step(name: str, source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Run one step; a refusal is recorded on the result, never raised."""

    step = dict(MINT_STEPS).get(name)
    if step is None:
        raise KeyError(f"no mint step {name!r}")
    try:
        # Savepoint-isolated: a DB error must not leave the caller's
        # transaction aborted for every later spec (found live at
        # Gate 4 - a caught mint error poisoned the whole apply run).
        with dest.begin_nested():
            return step(source, dest, ctx)
    except MintRefusal as exc:
        return MintResult(name, refused=str(exc))
    except sa.exc.SQLAlchemyError as exc:
        # A mint failure must land in the receipt, not kill the run
        # (found live at Gate 4: sales pipeline FK before tenants).
        return MintResult(name, refused=f"database error ({type(exc).__name__})")


def mint_all(source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> tuple[MintResult, ...]:
    return tuple(run_mint_step(name, source, dest, ctx) for name, _fn in MINT_STEPS)


__all__ = [
    "MAP_MINTS",
    "MINT_STEPS",
    "MintContext",
    "MintRefusal",
    "MintResult",
    "mint_all",
    "run_mint_step",
]
