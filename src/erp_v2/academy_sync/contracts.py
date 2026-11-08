"""Destination-owned app-sync contract for Academy learning completions.

ERP owns this capability schema (app-sync rule: the destination declares
what it accepts). The payload mirrors the completion subset of
Academy's live v2 training projection (`build_payload` in
`dotmac_academy_v2:app/services/erp_sync.py`) minus the `version`/`event`
keys, which the envelope's capability id and contract version replace.

Application spellings follow the app-sync proof contract
(`tests/unit/test_app_sync_contracts.py` in dotmac_starter_mt):
`dotmac_erp` owns, `dotmac_academy_app` sends. The latter is the Academy
product's stable source coordinate across its v1/v2 compositions.
"""

from __future__ import annotations

from typing import Final

from dotmac_app_sync import SyncContract, SyncContractRegistry

OWNER_APPLICATION: Final = "dotmac_erp"
SOURCE_APPLICATION: Final = "dotmac_academy_app"
CAPABILITY_ID: Final = "learning.completion.observation.v1"

COMPLETION_CONTRACT: Final = SyncContract(
    capability_id=CAPABILITY_ID,
    owner_application=OWNER_APPLICATION,
    summary="A staff course completion observed by Academy, recorded as an ERP certification.",
    payload_schema={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "employee_ref",
            "academy_course_ref",
            "course_title",
            "progress_pct",
            "status",
            "passed",
            "completed_on",
            "certificate_ref",
        ],
        "properties": {
            "employee_ref": {"type": "string", "minLength": 1, "maxLength": 30},
            "academy_enrollment_ref": {"type": ["string", "null"], "maxLength": 64},
            "academy_course_ref": {"type": "string", "minLength": 1, "maxLength": 120},
            "course_title": {"type": "string", "minLength": 1, "maxLength": 200},
            "course_version": {"type": ["integer", "null"]},
            "progress_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "status": {"const": "completed"},
            "occurred_at": {"type": ["string", "null"]},
            "passed": {"const": True},
            "completed_on": {"type": ["string", "null"]},
            "certificate_ref": {"type": ["string", "null"], "maxLength": 100},
        },
    },
)

CONTRACTS: Final = (COMPLETION_CONTRACT,)


def build_registry() -> SyncContractRegistry:
    return SyncContractRegistry(CONTRACTS)


__all__ = [
    "CAPABILITY_ID",
    "COMPLETION_CONTRACT",
    "CONTRACTS",
    "OWNER_APPLICATION",
    "SOURCE_APPLICATION",
    "build_registry",
]
