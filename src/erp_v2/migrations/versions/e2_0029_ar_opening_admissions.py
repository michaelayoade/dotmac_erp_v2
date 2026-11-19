"""PLACEHOLDER, DROP AT RE-CHAIN: stands in for #72 (AR opening admission, e2_0029) so the B-16 chain resolves in CI.

This revision does nothing. It exists only because B-16 (e2_0032) is stacked on migrations that are not merged yet.
The coordinator drops this file (and the commit that adds it) when re-chaining B-16 onto the real revision.
"""

from __future__ import annotations

revision = "e2_0029_ar_opening_admissions"
down_revision = "e2_0028_migration_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
