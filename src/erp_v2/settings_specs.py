"""ERP declarations consumed by the released kernel's browser presentation.

The reference application's settings feature is not part of the kernel wheel.
Declare these through its public registry so authenticated pages work without
importing reference-app code or seeding settings rows at serving startup.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import SettingSpec, register_specs


def _timezone(value: object) -> None:
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use a valid IANA display timezone") from exc


def _format(value: object) -> None:
    fmt = str(value)
    if not fmt or len(fmt) > 128 or "\x00" in fmt:
        raise ValueError("Use a bounded date/time format")
    directives = 0
    index = 0
    while index < len(fmt):
        if fmt[index] != "%":
            index += 1
            continue
        if index + 1 == len(fmt) or fmt[index + 1] not in "aAbBcdfGHIjmMpSuUVwWxXyYzZ%":
            raise ValueError("Unsupported date/time format directive")
        directives += fmt[index + 1] != "%"
        index += 2
    if not directives:
        raise ValueError("Date/time format must contain a directive")
    datetime(2026, 1, 31, 13, 45, tzinfo=UTC).strftime(fmt)


SPECS = [
    SettingSpec(
        domain=SettingDomain.branding,
        key="ui_branding",
        value_type=SettingValueType.json,
        default={},
        label="ERP branding overrides",
        description="Tenant branding values validated by the kernel presentation contract.",
    ),
    SettingSpec(
        domain=SettingDomain.display,
        key="timezone",
        value_type=SettingValueType.string,
        default="UTC",
        label="Display timezone",
        description="IANA timezone for browser presentation; API timestamps remain unchanged.",
        validator=_timezone,
    ),
    SettingSpec(
        domain=SettingDomain.display,
        key="date_format",
        value_type=SettingValueType.string,
        default="%Y-%m-%d",
        label="Date format",
        description="Portable strftime format used to display dates.",
        validator=_format,
    ),
    SettingSpec(
        domain=SettingDomain.display,
        key="datetime_format",
        value_type=SettingValueType.string,
        default="%Y-%m-%d %H:%M",
        label="Date and time format",
        description="Portable strftime format used to display timestamps.",
        validator=_format,
    ),
]

register_specs(SPECS)
