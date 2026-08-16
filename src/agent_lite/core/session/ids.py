from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

# New session IDs carry their creation timestamp so the storage layer can place
# them under ~/.kama/sessions/YYYY/MM/DD without reading meta.json first.
_SESSION_ID_RE = re.compile(
    r"^sess-(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})-"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})-"
    r"(?P<random>[0-9a-f]{12})$"
)


def new_session_id(now: datetime | None = None) -> str:
    """Return a timestamped, collision-resistant session ID."""
    timestamp = now or datetime.now(UTC)
    return f"sess-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"


def session_date_parts(session_id: str) -> tuple[str, str, str] | None:
    """Return (year, month, day) encoded in a new-format session ID."""
    match = _SESSION_ID_RE.fullmatch(session_id)
    if match is None:
        return None
    return match.group("year"), match.group("month"), match.group("day")
