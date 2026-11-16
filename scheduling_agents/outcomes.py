"""Render user-facing results from saved workflow records."""

from workflow_context import Booking, Outcome, Slot, Status, Task

from .inputs import zone_for


FIELD_NAMES = {
    "intent": "what you would like to do with your schedule",
    "eventTypeId": "the event type",
    "attendee.name": "the attendee's name",
    "attendee.email": "the attendee's email",
    "attendee.timeZone": "the attendee's IANA time zone (for example, America/New_York)",
    "timeZone": "the IANA time zone (for example, America/New_York)",
    "start": "the date and local start time",
    "range": "the start and end of the time window",
    "bookingReference": "a booking reference, attendee, title, or meeting date",
    "lengthInMinutes": "the meeting duration in minutes",
    "location": "the meeting location",
    "cancellationReason": "the cancellation reason",
}


def interval_label(slot: Slot, zone_name: str | None) -> str:
    zone = zone_for(zone_name) or zone_for("UTC")
    local = slot.local(zone)
    start = local.start.strftime("%b %d, %Y %I:%M %p (%Z)")
    end = local.end.strftime(
        "%I:%M %p (%Z)" if local.end.date() == local.start.date()
        else "%b %d, %Y %I:%M %p (%Z)",
    )
    return f"{start} to {end} [{zone.key}]"


def booking_label(booking: Booking, zone: str | None = None) -> str:
    name = booking.title or booking.name or "Booking"
    status = "pending, awaiting host confirmation" if booking.status == "pending" else booking.status
    return f"{name}, {interval_label(booking.slot, zone or booking.timeZone)} ({status}, {booking.calcom_uid})"


def finish(
    task: Task, status: Status, message: str, *,
    bookings: tuple[Booking, ...] = (), slots: tuple[Slot, ...] = (),
    candidates: tuple[Booking, ...] = (), fields: tuple[str, ...] = (),
    report_partial: bool = True,
):
    original = task.target_booking if task.intent == "reschedule_booking" else None
    cancellation = task.writes.get("cancel_booking")
    if report_partial and original and cancellation and cancellation.state == "applied" and not task.replacement:
        message = f"The original booking ({original.calcom_uid}) was cancelled. {message}"
    zone = zone_for(task.time_zone)
    outcome = Outcome(
        task.id, status, message, task.time_zone, bookings,
        tuple(slot.local(zone) for slot in slots) if zone else slots,
        candidates, fields, original, task.replacement,
        question_key=task.pending.key if status == "awaiting_input" and task.pending else None,
    )
    task.transition(task.role, "finished", outcome=outcome)
