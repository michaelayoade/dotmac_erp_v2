"""Alembic environment for the composed ERP assembly.

Composes the kernel base lineage plus each adopted module lineage
(currently files, imports, numbering). Bindings and plane selections
are installed before Alembic resolves any module lineage's
`depends_on` edges.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotmac_accounting import models as accounting_models  # noqa: F401
from dotmac_ai_operations import models as ai_operations_models  # noqa: F401
from dotmac_analytics import models as analytics_models  # noqa: F401
from dotmac_approvals import models as approvals_models  # noqa: F401
from dotmac_assets import models as assets_models  # noqa: F401
from dotmac_banking import models as banking_models  # noqa: F401
from dotmac_billing import models as billing_models  # noqa: F401
from dotmac_collections import models as collections_models  # noqa: F401
from dotmac_commercial_agreements import models as commercial_agreements_models  # noqa: F401
from dotmac_customers import models as customers_models  # noqa: F401
from dotmac_documents import models as documents_models  # noqa: F401
from dotmac_durable_timers import models as durable_timers_models  # noqa: F401
from dotmac_expenses import models as expenses_models  # noqa: F401
from dotmac_files import models as files_models  # noqa: F401
from dotmac_finance import models as finance_models  # noqa: F401
from dotmac_forms import models as forms_models  # noqa: F401
from dotmac_imports import models as imports_models  # noqa: F401
from dotmac_inbox import models as inbox_models  # noqa: F401
from dotmac_inventory import models as inventory_models  # noqa: F401
from dotmac_kernel import (
    audit,  # noqa: F401
    entitlements,  # noqa: F401
    idempotency_models,  # noqa: F401
    models_platform,  # noqa: F401
    settings_models,  # noqa: F401
)
from dotmac_kernel.messaging import models as messaging_models  # noqa: F401
from dotmac_kernel.migrations.configuration import set_alembic_database_url
from dotmac_kernel.models import Base
from dotmac_kernel.planes import install_module_plane_selections
from dotmac_kernel.prerequisites import install_prerequisite_bindings
from dotmac_numbering import models as numbering_models  # noqa: F401
from dotmac_party import models as party_models  # noqa: F401
from dotmac_payables import models as payables_models  # noqa: F401
from dotmac_payments import models as payments_models  # noqa: F401
from dotmac_payroll import models as payroll_models  # noqa: F401
from dotmac_people import models as people_models  # noqa: F401
from dotmac_procurement import models as procurement_models  # noqa: F401
from dotmac_projects import models as projects_models  # noqa: F401
from dotmac_sales import models as sales_models  # noqa: F401
from dotmac_tax import models as tax_models  # noqa: F401
from dotmac_ticketing import models as ticketing_models  # noqa: F401
from dotmac_workflow_runtime import models as workflow_runtime_models  # noqa: F401
from sqlalchemy import engine_from_config, pool

from erp_v2.hr import models as hr_models  # noqa: F401
from erp_v2.migration_bindings import (
    ASSEMBLY_MODULE_PLANES,
    ASSEMBLY_PREREQUISITE_BINDINGS,
)

config = context.config

held_url = config.get_main_option("erp_v2_migration_url")
if not held_url:
    held_url = os.getenv("MIGRATION_DATABASE_URL")
if held_url:
    set_alembic_database_url(config, database_url=held_url)

install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
install_module_plane_selections(config.attributes.get("module_plane_selections", ASSEMBLY_MODULE_PLANES))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
