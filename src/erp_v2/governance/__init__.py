"""Per-tenant governance policy (D-9): optional dual control, default OFF.

Michael's ruling (2026-11-17/18): no compulsory two-person. Every coded two-person / separation-of-duties
check in this assembly is a per-tenant policy that defaults to OFF; a missing policy row reads as OFF.
The policy is written only by the audited operator CLI (``erp_v2.governance.cli``); the runtime role can
only read it. See ``erp_v2.governance.policy``.
"""
