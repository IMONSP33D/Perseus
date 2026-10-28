"""current_time: the date and time, in UTC or an IANA time zone."""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from perseus.tools import ToolFailure, tool


@tool(description="Get the current date and time, in UTC or in an IANA time zone such as 'America/New_York'.",
      parameters={"type": "object", "properties": {
          "timezone": {"type": "string", "maxLength": 64, "default": "UTC",
                       "description": "IANA time zone name; default UTC."}}},
      timeout_s=5)
def current_time(ctx, timezone: str = "UTC"):
    try:
        tz = _tz.utc if timezone.upper() == "UTC" else ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolFailure(f"unknown time zone {timezone!r}; use an IANA name like 'Europe/Paris'")
    now = datetime.now(tz)
    return {"timezone": timezone, "iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")}
