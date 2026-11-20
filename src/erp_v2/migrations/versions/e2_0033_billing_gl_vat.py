"""PLACEHOLDER, DROP AT RE-CHAIN: stands in for B-15 rev 2 (billing GL VAT, e2_0033) so the stacked customers chain resolves.

This revision does nothing. It exists only because the customer screen (e2_0035) is stacked on migrations that are not
merged yet. The coordinator drops this file (and the commit that adds it) when re-chaining onto the real revision.
"""

from __future__ import annotations

revision = "e2_0033_billing_gl_vat"
down_revision = "e2_0032_ar_producer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
