"""ERP v2 assembly spec — waves 1-6: all released modules composed.

Ruled 2026-11-08 (Michael): ERP moves to this new Starter-composed app,
replace-not-adapt; legacy `dotmac_erp` is never mounted into it and
remains untouched as rollback until the one downtime flip. The goal is
an ERP fully composable from the reusable Starter modules: this spec
is that composition. Classification: `docs/classification.md`.

Composition posture:
- 31 Starter modules mounted at their latest RELEASED pins (waves
  1-6 in one composition; data waves still land in wave order — see
  `docs/cutover.md`). Mounting is not a flip: no consumer reads a
  module through this app, and no ERP-unique slice has ported yet,
  until that module's transform mapping rehearses. Only unreleased
  or non-mountable Starter surfaces stay out: `dotmac-workforce`
  (no release), connectors (libraries for product adapters, no
  module manifest), and later unreleased bumps of pinned modules
  (e.g. numbering a4, accounting a2, billing a11, payments a4).
- `tenancy="multi"`: ERP is a multi-tenant product (legacy
  `product_assembly.py` declares the same), declared explicitly rather
  than inherited from the kernel default.
- Web surface mounts when a database is bound (tenant resolution and
  identity require one) unless `ERP_WEB_ENABLED=false` forces
  API-only: one tenant facet at `/` with kernel-cookie login
  (`erp_v2.web`), admission `erp.web.access`. Identity, sessions and
  credentials are consumed from the kernel; ERP stores none. The
  no-DSN shell therefore stays API/health-only by construction.
- Explicit database binding (`erp_v2.database`): if `DATABASE_URL` is
  declared, the assembly binds its own `DatabaseRuntime` and a startup
  check proves the DSN role is non-superuser + NOBYPASSRLS. Absent DSN
  = empty-shell mode (health only; `/health` is DB-free by kernel
  design); blank/non-Postgres DSN refuses at import. When a DSN is
  declared, `require_database_runtime` is True (no reference-runtime
  fallback) and `/health/ready` probes the bound engine.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotmac_accounting.manifest import module as accounting_module
from dotmac_ai_operations.manifest import module as ai_operations_module
from dotmac_analytics.manifest import module as analytics_module
from dotmac_approvals.manifest import module as approvals_module
from dotmac_assets.manifest import module as assets_module
from dotmac_banking.manifest import module as banking_module
from dotmac_billing.manifest import module as billing_module
from dotmac_collections.manifest import module as collections_module
from dotmac_commercial_agreements.manifest import module as commercial_agreements_module
from dotmac_customers.manifest import module as customers_module
from dotmac_documents.manifest import module as documents_module
from dotmac_durable_timers.manifest import module as durable_timers_module
from dotmac_expenses.manifest import module as expenses_module
from dotmac_files.manifest import module as files_module
from dotmac_finance.manifest import module as finance_module
from dotmac_forms.manifest import module as forms_module
from dotmac_imports.manifest import module as imports_module
from dotmac_inbox.manifest import module as inbox_module
from dotmac_inventory.manifest import module as inventory_module
from dotmac_kernel.api_documentation import environment_api_documentation_policy
from dotmac_kernel.assembly import ProductAssemblySpec, ProductSecurityPolicy
from dotmac_kernel.permission_provisioning import RoleDefinition, RoleGrant, RoleGrantProfile
from dotmac_kernel.web_deps import TENANT_COOKIE_AUTHENTICATION
from dotmac_kernel.web_surfaces import (
    AuthenticationProfileBinding,
    BrowserSecurityPlane,
    BrowserSessionPolicy,
    NavigationRegion,
    TemplateRef,
    WebFacetMount,
    WebRouteRef,
)
from dotmac_numbering.manifest import module as numbering_module
from dotmac_party.manifest import module as party_module
from dotmac_payables.manifest import module as payables_module
from dotmac_payments.manifest import module as payments_module
from dotmac_payroll.manifest import module as payroll_module
from dotmac_people.manifest import module as people_module
from dotmac_procurement.manifest import module as procurement_module
from dotmac_projects.manifest import module as projects_module
from dotmac_sales.manifest import module as sales_module
from dotmac_tax.manifest import module as tax_module
from dotmac_ticketing.manifest import module as ticketing_module
from dotmac_ui import UI_CONTRACT_VERSION, static_dir
from dotmac_workflow_runtime.manifest import module as workflow_runtime_module

from erp_v2 import ui
from erp_v2.database import build_database_runtime, database_startup_checks
from erp_v2.migration_bindings import ASSEMBLY_MODULE_PLANES
from erp_v2.readiness import readiness_feature
from erp_v2.web.routes import ACCESS_PERMISSION, erp_web_feature

PRODUCT_NAME: Final = "dotmac-erp"

_ROOT: Final = Path(__file__).resolve().parent
_WEB_ENABLED: Final = os.environ.get("ERP_WEB_ENABLED", "true").lower() not in (
    "false",
    "0",
    "no",
)

# Built at import like the reference assembly's settings reads. Engine
# construction opens no connection; posture is proven by the startup check.
_database_runtime = build_database_runtime()

assembly: Final = ProductAssemblySpec(
    name=PRODUCT_NAME,
    # Order canonical: readiness feature first, then product modules
    # alphabetically by module code (legacy product_assembly convention).
    modules=(
        readiness_feature,
        erp_web_feature,
        accounting_module,
        commercial_agreements_module,
        ai_operations_module,
        analytics_module,
        approvals_module,
        assets_module,
        banking_module,
        billing_module,
        collections_module,
        customers_module,
        documents_module,
        durable_timers_module,
        expenses_module,
        files_module,
        finance_module,
        forms_module,
        imports_module,
        inbox_module,
        inventory_module,
        numbering_module,
        party_module,
        payables_module,
        payments_module,
        payroll_module,
        people_module,
        procurement_module,
        projects_module,
        sales_module,
        tax_module,
        ticketing_module,
        workflow_runtime_module,
    ),
    module_planes=ASSEMBLY_MODULE_PLANES,
    tenancy="multi",
    platform_surface_enabled=False,
    web_enabled=_WEB_ENABLED and _database_runtime is not None,
    ui_contract_version=UI_CONTRACT_VERSION,
    web_facets=(
        WebFacetMount(
            code="staff_admin",
            url_prefix="/",
            shell=TemplateRef("base.html"),
            authentication_profile="erp_tenant_session",
            admission_permission=ACCESS_PERMISSION,
            navigation_regions=(NavigationRegion("primary"),),
            entry_routes=(
                WebRouteRef("erp_web", "legacy", "login_page"),
                WebRouteRef("erp_web", "legacy", "login_submit"),
                WebRouteRef("erp_web", "legacy", "logout"),
                WebRouteRef("erp_web", "legacy", "home"),
            ),
            login_route=WebRouteRef("erp_web", "legacy", "login_page"),
            landing_route=WebRouteRef("erp_web", "legacy", "home"),
            logout_route=WebRouteRef("erp_web", "legacy", "logout"),
        ),
    ),
    authentication_profiles=(
        AuthenticationProfileBinding(
            code="erp_tenant_session",
            provider=TENANT_COOKIE_AUTHENTICATION,
            session=BrowserSessionPolicy(cookie_name="access_token", cookie_path="/"),
            security_plane=BrowserSecurityPlane.TENANT,
        ),
    ),
    role_definitions=(RoleDefinition("admin"), RoleDefinition("staff")),
    role_grant_profiles=(
        RoleGrantProfile(
            code="erp.web",
            version=1,
            grants=(
                RoleGrant("admin", ACCESS_PERMISSION),
                RoleGrant("staff", ACCESS_PERMISSION),
            ),
        ),
    ),
    assembly_template_dir=_ROOT / "templates",
    assembly_static_dir=_ROOT / "static",
    packaged_static_dirs=(static_dir(),),
    stylesheets=(ui.UI_STYLESHEET_URL,),
    database_runtime=_database_runtime,
    require_database_runtime=_database_runtime is not None,
    startup_checks=database_startup_checks(_database_runtime),
    startup_hooks=(),
    security_policy=ProductSecurityPolicy(),
    # Kernel fails closed without an explicit docs policy; resolve from
    # ENVIRONMENT (production = docs off), as the reference assembly does.
    api_documentation=environment_api_documentation_policy(),
)

database_runtime = _database_runtime

__all__ = ["PRODUCT_NAME", "assembly", "database_runtime"]
