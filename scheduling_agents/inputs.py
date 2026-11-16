"""Validate extracted scheduling inputs and resolve local times."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workflow_context import Account, EventType, Slot, Task


EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
RELATIVE_DATE = re.compile(r"D([+-]\d{1,3})(T\d{2}:\d{2}(?::\d{2})?)?\Z")
TIME_FIELDS = ("start", "range_start", "range_end", "target_start", "target_end")
WRITE_VERBS = {
    "create_booking": r"book|schedule|set up|arrange|reserve|make (?:a|the) (?:booking|appointment)",
    "cancel_booking": r"cancel|call off|remove|delete",
    "reschedule_booking": r"reschedule|rebook|move|change|push (?:back|out)|bring forward",
}
def supports_write(message: str, intent: str) -> bool:
    verbs = WRITE_VERBS[intent]
    if re.search(r"\b(?:should i|how (?:do|can|would) i|what if i)\b", message, re.I):
        return False
    return bool(re.search(rf"\b(?:{verbs})\b", message, re.I)) and not re.search(
        rf"\b(?:do not|don't|dont|not|never|without|avoid)\s+(?:actually\s+)?(?:{verbs})\b",
        message, re.I,
    )


def zone_for(value: str | None) -> ZoneInfo | None:
    try:
        return ZoneInfo(value) if value else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def event_for(account: Account, value: str | None, *, default=True) -> EventType | None:
    if value:
        matches = [
            event for event in account.event_types
            if value.strip().casefold() in (event.name.casefold(), str(event.id))
        ]
        return matches[0] if len(matches) == 1 else None
    if default and len(account.event_types) == 1:
        return account.event_types[0]
    return None


def local_instants(value: str, zone: ZoneInfo, *, allow_date=False) -> tuple[datetime, ...]:
    if not value or (DATE.fullmatch(value) and not allow_date):
        return ()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return (parsed.astimezone(timezone.utc),)
        instants = {
            parsed.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            for fold in (0, 1)
        }
        return tuple(sorted(
            value for value in instants
            if value.astimezone(zone).replace(tzinfo=None) == parsed
        ))
    except (ValueError, OverflowError):
        return ()


def interval(start: str | None, end: str | None, zone: ZoneInfo | None) -> Slot | None:
    if not (start and end and zone):
        return None
    starts = local_instants(start, zone, allow_date=True)
    ends = local_instants(end, zone, allow_date=True)
    if len(starts) == len(ends) == 1 and starts[0] < ends[0]:
        return Slot(starts[0], ends[0])
    return None


def remaining_day(slot: Slot, zone: ZoneInfo) -> Slot:
    next_day = slot.start.astimezone(zone).date() + timedelta(days=1)
    midnight = datetime.combine(next_day, time(), zone).astimezone(timezone.utc)
    return Slot(slot.start, midnight)


def expand_relative_dates(values: dict, zone: ZoneInfo | None, clock: datetime):
    if zone is None:
        return
    today = clock.astimezone(zone).date()
    for key in TIME_FIELDS:
        match = RELATIVE_DATE.fullmatch(values.get(key) or "")
        if match:
            offset, clock_time = match.groups()
            values[key] = (today + timedelta(days=int(offset))).isoformat() + (clock_time or "")


def resolve_input_times(task: Task, now: datetime):
    values = task.inputs.model_dump(include=set(TIME_FIELDS))
    original = values.copy()
    expand_relative_dates(values, zone_for(task.time_zone), task.received_at or now)
    if values != original:
        task.inputs = task.inputs.model_copy(update=values)


def text_matches(clue: str, value: str) -> bool:
    tokens = re.findall(r"\w+", clue.casefold())
    available = set(re.findall(r"\w+", value.casefold()))
    return bool(tokens) and all(token in available for token in tokens)


def source_contains(message: str, value: str) -> bool:
    return bool(re.search(r"(?<![\w@.-])" + re.escape(value) + r"(?![\w@-]|\.\w)", message, re.I))
