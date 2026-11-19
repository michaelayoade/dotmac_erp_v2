"""PLACEHOLDER, DROP AT RE-CHAIN: stands in for B-14 (migration holdings, e2_0030) so the B-16 chain resolves in CI.

This revision does nothing. It exists only because B-16 (e2_0032) is stacked on migrations that are not merged yet.
The coordinator drops this file (and the commit that adds it) when re-chaining B-16 onto the real revision.
"""

from __future__ import annotations

revision = "e2_0030_migration_holdings"
down_revision = "e2_0029_ar_opening_admissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
