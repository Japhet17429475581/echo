import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_after(value: str | None, maximum: float, current: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            delay = (dt - (current or datetime.now(UTC))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return min(delay, maximum)
