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
                "is_published": True,
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
    bands = list(
        source.execute(
            sa.text(
                "SELECT tax_band_id, organization_id, name, min_amount, max_amount, rate, sequence "
                "FROM tax_band WHERE is_active"
            )
        ).mappings()
    )
    if bands and not ctx.tax_band_rule_binding:
        unbound = sorted({str(b["name"]) for b in bands})
        result.blockers.append(f"tax bands not minted: no tax_band_rule_binding for band names {unbound}")
        return result
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
            sa.text("SELECT customer_id, organization_id, legal_name, trading_name, currency_code FROM customer")
        ).mappings()
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


MINT_STEPS: Final[tuple[tuple[str, Any], ...]] = (
    ("kernel_tenants", mint_kernel_tenants),
    ("pay_structure_revisions", mint_pay_structure_revisions),
    ("pay_rule_bases", mint_pay_rule_bases),
    ("tax_authorities", mint_tax_authorities),
    ("tax_rules", mint_tax_rules),
    ("sourcing_event_lines", mint_sourcing_event_lines),
    ("sales_leads", mint_sales_leads),
    ("document_libraries_types", mint_document_libraries_types),
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
}


def run_mint_step(name: str, source: sa.Connection, dest: sa.Connection, ctx: MintContext) -> MintResult:
    """Run one step; a refusal is recorded on the result, never raised."""

    step = dict(MINT_STEPS).get(name)
    if step is None:
        raise KeyError(f"no mint step {name!r}")
    try:
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
