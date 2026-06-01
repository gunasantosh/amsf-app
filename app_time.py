import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

APP_TIMEZONE_NAME = os.getenv("AMSF_TIMEZONE", "Asia/Kolkata")
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)


def utc_now() -> datetime:
    """Return naive UTC for SQLite storage and UTC comparisons."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now() -> datetime:
    """Return naive application-local time for calendar decisions."""
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def as_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    utc_value = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return utc_value.astimezone(APP_TIMEZONE)


def format_local_datetime(dt: datetime | None) -> str:
    local_value = as_local(dt)
    return local_value.strftime("%Y-%m-%d %H:%M %Z") if local_value else "-"


def format_local_date(dt: datetime | None) -> str:
    local_value = as_local(dt)
    return local_value.strftime("%Y-%m-%d") if local_value else "-"

