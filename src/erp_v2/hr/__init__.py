"""ERP-unique HR domain (the `e2` lineage).

Everything under this package is product code with no Starter owner:
leave, attendance, discipline, appraisals, recruitment, training,
onboarding. Directory facts (employees, departments, positions) are
never redefined here — they are consumed from `dotmac-people` by
composite tenant foreign key. First slice: leave.
"""

from erp_v2.hr import models

__all__ = ["models"]
