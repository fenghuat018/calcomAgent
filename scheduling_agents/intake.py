"""Validate intent proposals and answers against the active conversation turn."""

import re
from calendar import month_abbr, month_name
from datetime import datetime, timedelta

from tools.calcom_client import UID
from workflow_context import Answer, Proposal, READ_INTENTS, WRITE_INTENTS, WorkflowContext, iso

from .inputs import DATE, EMAIL, TIME_FIELDS, event_for, expand_relative_dates, source_contains, supports_write, zone_for
from .outcomes import finish
from .state import ask


ANSWERS = {
    "eventTypeId": ("event_type",),
    "attendee.name": ("attendee_name",),
    "attendee.email": ("attendee_email",),
    "attendee.timeZone": ("time_zone",),
    "timeZone": ("time_zone",),
    "start": ("start",),
    "range": ("range_start", "range_end"),
    "bookingReference": ("uid", "numeric_id", "attendee_clue", "title_clue", "target_start", "target_end"),
    "lengthInMinutes": ("duration",),
    "location": ("location",),
    "cancellationReason": ("cancellation_reason",),
}
GENERIC_BOOKING_LABELS = {
    "appointment", "appointments", "booking", "bookings", "call", "calls", "calendar",
    "event", "events", "meeting", "meetings", "my appointment", "my booking", "my call", "my meeting",
    "slot", "slots",
}
EXPLICIT_INTENT = {
    "create_booking": r"(?:book|schedule|reserve|set up)\b",
    "cancel_booking": r"(?:cancel|call off)\b",
    "reschedule_booking": r"(?:reschedule|move|rebook)\b",
    "list_bookings": r"list\b",
    "get_availability": r"(?:show|find|when)\b.*\b(?:available|availability|open slots)\b",
    "get_booking": r"(?:show (?:me )?details|when and where)\b",
}
INTENT_PREFIX = r"^\s*(?:(?:please|can you|could you)\s+)?"
GENERAL_HELP = re.compile(
    r"(?:i (?:need|want)(?: some)? help|(?:can|could) you help(?: me)?)\s+"
    r"with\s+(?:(?:my|the)\s+)?(?:schedule|scheduling|calendar|appointments|bookings)[.!?]?",
    re.I,
)
EXPLICIT_OFFSET = re.compile(r"(?:UTC|GMT)\s*[+-]\d{1,2}\b|\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})\b", re.I)
ZONE_SUFFIX = re.compile(r"\s+[A-Za-z_+-]+(?:/[A-Za-z0-9_+-]+)+$")
IANA_ZONE = re.compile(r"\b[A-Za-z_+-]+(?:/[A-Za-z0-9_+-]+)+\b|\bUTC\b(?!\s*[+-]\d)", re.I)
CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*(?:AM|PM)|\d{1,2}:\d{2}|noon|midnight)"
ORDINAL_CHOICE = re.compile(
    rf"(?:the\s+)?(first|second|third|fourth|fifth|last)"
    rf"(?:\s+(?:one|option|occurrence|{CLOCK}))?"
    r"(?:,?\s+(?:before|after) the clocks go (?:back|forward))?"
    r"(?:\s*\((?:UTC|GMT)\s*[+-]\d{1,2}(?::?\d{2})?\))?[.!]?",
    re.I,
)
NUMBERED_CHOICE = re.compile(
    r"(?:(?:i(?:'ll| will) take|choose|pick|select|book|use)\s+(?:the\s+)?)?"
    r"(?:(?:option|choice|number)\s+)?#?(\d+)[.!]?", re.I,
)
CHOICE_OFFSET = re.compile(r"\b(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?\b", re.I)
ORDINALS = ("first", "second", "third", "fourth", "fifth")
MONTHS = {name.casefold(): month for names in (month_name, month_abbr)
          for month, name in enumerate(names) if month}
WRITTEN_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})\b|\b(" + "|".join(MONTHS)
    + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+)(\d{4})\b", re.I,
)
WRITTEN_RANGE = re.compile(rf"\bfrom\s+({CLOCK})\s+(?:to|until|through)\s+({CLOCK})\b", re.I)


def grounded(proposal: Proposal, message: str, previous_uid=None) -> Proposal:
    fields = proposal.model_dump()
    if proposal.uid and (
        not UID.fullmatch(proposal.uid)
        or not (source_contains(message, proposal.uid) or proposal.uid == previous_uid)
    ):
        fields["uid"] = None
    if proposal.numeric_id is not None and (
        proposal.numeric_id <= 0 or not source_contains(message, str(proposal.numeric_id))
    ):
        fields["numeric_id"] = None
    if fields["uid"] and fields["uid"].isdigit():
        fields["numeric_id"] = int(fields.pop("uid"))
        if not fields["numeric_id"]:
            fields["numeric_id"] = None
    if proposal.attendee_email and (
        not EMAIL.fullmatch(proposal.attendee_email) or not source_contains(message, proposal.attendee_email)
    ):
        fields["attendee_email"] = None
    return Proposal.model_validate(fields)


def normalize_request(ctx: WorkflowContext, proposal: Proposal) -> Proposal:
    message = ctx.task.message
    if GENERAL_HELP.fullmatch(message):
        return Proposal()
    fields = grounded(proposal, message, ctx.referenced_uid).model_dump()
    intent = fields["intent"]
    if intent == "unclear":
        intent = next((
            item for item, pattern in EXPLICIT_INTENT.items()
            if re.search(INTENT_PREFIX + pattern, message, re.I)
        ), intent)
        fields["intent"] = intent
    for key in ("event_type", "title_clue"):
        value = fields[key]
        if value and value.strip().casefold() in GENERIC_BOOKING_LABELS:
            if key == "title_clue" or not event_for(ctx.account, value, default=False):
                fields[key] = None
    if intent == "create_booking":
        fields["start"] = fields["start"] or fields["target_start"]
        if not fields["range_end"]:
            fields["start"] = fields["start"] or fields["range_start"]
            if fields["start"] == fields["range_start"]:
                fields["range_start"] = None
    elif intent in ("list_bookings", "get_availability"):
        fields["range_start"] = fields["range_start"] or fields["target_start"] or fields["start"]
        fields["range_end"] = fields["range_end"] or fields["target_end"]
    elif intent in ("get_booking", "cancel_booking", "reschedule_booking"):
        fields["attendee_clue"] = fields["attendee_clue"] or fields["attendee_name"]
        if intent == "reschedule_booking":
            fields["start"] = fields["start"] or fields["range_end"]
            if not re.search(r"\b(?:for|attendee)\s+(?:a\s+new\s+)?", message, re.I):
                fields["attendee_name"] = None
        else:
            fields["target_start"] = fields["target_start"] or fields["range_start"] or fields["start"]
            fields["target_end"] = fields["target_end"] or fields["range_end"]

    explicit_zones = {match for match in IANA_ZONE.findall(message) if zone_for(match)}
    if len(explicit_zones) == 1:
        fields["time_zone"] = explicit_zones.pop()
    zone = zone_for(fields["time_zone"] or ctx.saved_time_zone or ctx.account.default_time_zone)
    for key in TIME_FIELDS:
        if fields[key]:
            fields[key] = local_time_proposal(fields[key], zone, explicit_offset=bool(EXPLICIT_OFFSET.search(message)))
    for start_key, end_key in (("target_start", "target_end"), ("range_start", "range_end")):
        start, end = fields[start_key], fields[end_key]
        if start and not end and DATE.fullmatch(start):
            try:
                fields[end_key] = (datetime.fromisoformat(start).date() + timedelta(days=1)).isoformat()
            except ValueError:
                pass
    if intent in ("list_bookings", "get_availability") and not EXPLICIT_OFFSET.search(message):
        window = written_window(message)
        if window:
            fields["range_start"], fields["range_end"] = window
    return Proposal.model_validate(fields)


def written_window(message: str) -> tuple[str, str] | None:
    dates = WRITTEN_DATE.findall(message)
    window = WRITTEN_RANGE.search(message)
    if len(dates) != 1 or window is None:
        return None
    iso_date, month, day, year = dates[0]
    try:
        date = (
            datetime.fromisoformat(iso_date)
            if iso_date else datetime(int(year), MONTHS[month.casefold()], int(day))
        )
        bounds = []
        for value in window.groups():
            clock = value.casefold().replace(" ", "")
            if clock in ("noon", "midnight"):
                clock = "12:00" if clock == "noon" else "00:00"
            pattern = "%I:%M%p" if ":" in clock else "%I%p"
            if not clock.endswith(("am", "pm")):
                pattern = "%H:%M"
            stamp = datetime.strptime(clock, pattern)
            bounds.append(date.replace(hour=stamp.hour, minute=stamp.minute).isoformat())
        return bounds[0], bounds[1]
    except ValueError:
        return None


def local_time_proposal(value, zone, *, explicit_offset: bool) -> str:
    value = ZONE_SUFFIX.sub("", value.strip())
    if explicit_offset or "T" not in value and " " not in value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.utcoffset() is not None:
        if zone is not None:
            parsed = parsed.astimezone(zone)
        return parsed.replace(tzinfo=None).isoformat()
    return value


def route_intent(ctx: WorkflowContext, *, evidence: str, question_key: str | None = None):
    task = ctx.task
    intent = task.inputs.intent
    if intent in WRITE_INTENTS and not supports_write(evidence, intent):
        intent = "unclear"
    task.intent = intent
    task.time_zone = (
        task.inputs.time_zone if task.inputs.time_zone is not None
        else ctx.saved_time_zone or ctx.account.default_time_zone
    )
    task.reference_uid = task.inputs.uid
    if intent in WRITE_INTENTS:
        task.transfer("booking", "prepare_booking", "The user requested a booking change.",
                      task.save("intent", task.inputs), question_key=question_key)
    elif intent in READ_INTENTS:
        task.transfer("retrieval", "prepare_retrieval", "The user requested scheduling information.",
                      task.save("intent", task.inputs), question_key=question_key)
    else:
        ask(task, "triage", "route_intent", fields=("intent",))


def save_intent(ctx: WorkflowContext, proposal: Proposal):
    task = ctx.task
    task.inputs = normalize_request(ctx, proposal)
    if (
        task.inputs.intent == "create_booking" and ctx.referenced_availability
        and proposal.choice_from_availability
    ):
        event_id, zone, slots = ctx.referenced_availability
        index = proposal.choice_from_availability
        if 1 <= index <= len(slots):
            slot = slots[index - 1]
            task.inputs.start = iso(slot.start)
            task.inputs.event_type = str(event_id)
            task.inputs.time_zone = zone
            task.inputs.duration = round((slot.end - slot.start).total_seconds() / 60)
    route_intent(ctx, evidence=task.message)


def save_question(ctx: WorkflowContext):
    task = ctx.task
    question = task.pending
    if not question or question.revision != task.revision:
        raise ValueError("No current question to publish")
    if question.reconciliation_notice:
        task.pending = None
        return finish(task, "recoverable", question.message, bookings=question.bookings)
    finish(task, "awaiting_input", question.message, slots=question.slots,
           candidates=question.bookings, fields=question.fields)


def selected_answer(ctx: WorkflowContext, choice: int) -> bool:
    task, question = ctx.task, ctx.task.pending
    if choice <= 0:
        return False
    if question.bookings and choice <= len(question.bookings):
        task.selected_booking = question.bookings[choice - 1]
        task.reference_uid = task.selected_booking.calcom_uid
        return True
    if question.slots and choice <= len(question.slots):
        task.selected_slot = question.slots[choice - 1]
        task.inputs = task.inputs.model_copy(update={"start": iso(task.selected_slot.start)})
        return True
    return False


def saved_choice(ctx: WorkflowContext, proposed: int | None) -> int | None:
    task, question = ctx.task, ctx.task.pending
    slots = question.slots or tuple(booking.slot for booking in question.bookings)
    if not slots:
        return proposed
    message = task.message.strip()
    ordinal = ORDINAL_CHOICE.fullmatch(message)
    numbered = NUMBERED_CHOICE.fullmatch(message)
    offset = CHOICE_OFFSET.search(message)
    choice = None
    if ordinal:
        word = ordinal.group(1).casefold()
        choice = len(slots) if word == "last" else ORDINALS.index(word) + 1
    if numbered:
        number = int(numbered.group(1))
        if choice is not None and number != choice:
            return 0
        choice = number
    if offset and (choice is not None or proposed is not None):
        zone = zone_for(task.time_zone)
        if not zone:
            return 0
        sign, hours, minutes = offset.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
        expected = -delta if sign == "-" else delta
        choices = [
            index for index, slot in enumerate(slots, 1)
            if slot.start.astimezone(zone).utcoffset() == expected
            and index == (choice if choice is not None else proposed)
        ]
        return choices[0] if len(choices) == 1 else 0
    return choice if choice is not None else proposed


def apply_fields(ctx: WorkflowContext, proposal: Proposal) -> bool:
    task, question = ctx.task, ctx.task.pending
    supplied = grounded(proposal, task.message).model_dump(exclude_none=True)
    permitted = {name for field in question.fields for name in ANSWERS.get(field, ())}
    if question.bookings:
        permitted.update(ANSWERS["bookingReference"])
    if question.slots:
        permitted.update(ANSWERS["range"])
    changes = {name: value for name, value in supplied.items() if name in permitted and value != ""}
    if "time_zone" in changes and not zone_for(changes["time_zone"]):
        del changes["time_zone"]
    if "range_start" in changes or "range_end" in changes:
        if not (changes.get("range_start") and changes.get("range_end")):
            return False
        if question.clears_slot:
            changes["start"] = None
    if "range" in question.fields and "range_start" not in changes and "start" not in changes:
        if not (permitted - set(ANSWERS["range"])).intersection(changes):
            return False
    missing_custom = {field.split(".", 1)[1] for field in question.fields if field.startswith("bookingFieldsResponses.")}
    custom = {entry.field: entry for entry in task.inputs.booking_fields or ()}
    accepted_custom = [
        entry for entry in proposal.booking_fields or ()
        if entry.field in missing_custom and entry.value.strip()
    ]
    if accepted_custom:
        custom.update({entry.field: entry for entry in accepted_custom})
        changes["booking_fields"] = list(custom.values())
    if not changes:
        return False
    if "time_zone" in changes:
        task.time_zone = changes["time_zone"]
    expand_relative_dates(changes, zone_for(task.time_zone), ctx.clock())
    if "start" in changes and changes["start"] is None:
        task.selected_slot = None
    reference_names = set(ANSWERS["bookingReference"])
    if reference_names.intersection(changes):
        task.reference_uid = changes.get("uid")
        task.selected_booking = None
        task.target_booking = None
        if task.reference_uid or changes.get("numeric_id"):
            for name in reference_names.difference(changes):
                changes[name] = None
        elif "target_start" in changes and "target_end" not in changes:
            changes["target_end"] = None
    task.inputs = task.inputs.model_copy(update=changes)
    return True


def save_answer(ctx: WorkflowContext, answer: Answer):
    task = ctx.task
    question = task.pending
    if not question or question.revision != task.revision:
        raise ValueError("The answer does not belong to the current question")
    if question.fields == ("intent",):
        proposal = normalize_request(ctx, answer.fields) if answer.fields else None
        valid = bool(proposal and (
            proposal.intent in READ_INTENTS
            or (proposal.intent in WRITE_INTENTS and supports_write(task.message, proposal.intent))
        ))
        if valid:
            task.inputs = proposal
    else:
        choice = saved_choice(ctx, answer.choice)
        valid = (
            selected_answer(ctx, choice) if choice is not None
            else answer.fields is not None and apply_fields(ctx, answer.fields)
        )
    if not valid:
        return save_question(ctx)
    task.transition("confirmation", "save_answer", answer=question)
    task.fresh_revision = None
    task.slot_preflight = None
    task.failure_key = None
    task.failed_kind = None
    task.reconcile_write = None
    task.recovery_result = None
    task.reconciled_booking = None
    task.read = None
    if zone_for(task.time_zone):
        ctx.saved_time_zone = task.time_zone
    task.transfer(question.destination, question.step, "A validated answer is ready.",
                  task.save("answer", answer), question_key=question.key, continuation=True)
