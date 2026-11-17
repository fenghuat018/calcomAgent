"""Render user-facing results from saved workflow records."""

from workflow_context import Booking, Outcome, Slot, Status, Task

from .inputs import zone_for


FIELD_PROMPTS = {
    "intent": "Tell me what you’d like to do, such as “list my bookings for next week” or “book a meeting”.",
    "eventTypeId": "Give the exact name of the Cal.com event type you’d like to use.",
    "attendee.name": "Give the name of the person attending the meeting.",
    "attendee.email": "Give the attendee’s email address, such as alex@example.com.",
    "attendee.timeZone": "Give the attendee’s time zone, such as America/New_York.",
    "timeZone": "Tell me which time zone to use for these dates, such as America/New_York.",
    "start": "Give the meeting’s date and start time, such as “tomorrow at 10 AM”.",
    "range": "Give the date and both ends of the time window, such as “tomorrow from 2 PM to 4 PM”.",
    "bookingReference": "Give the booking reference, or describe the meeting by its attendee, title, or date.",
    "lengthInMinutes": "Tell me how many minutes the meeting should last.",
    "location": "Tell me where the meeting should take place.",
    "cancellationReason": "Give the reason for cancelling the meeting.",
}


def clarification_prompt(fields: tuple[str, ...], event_names: tuple[str, ...] = ()) -> str:
    prompts = []
    for field in fields:
        if field == "eventTypeId" and event_names:
            choices = ", ".join(f"“{name}”" for name in event_names)
            prompts.append(f"Choose a Cal.com event type from {choices}.")
        else:
            name = field.removeprefix("bookingFieldsResponses.")
            prompts.append(FIELD_PROMPTS.get(field, f"Give a value for the “{name}” booking field."))
    if len(prompts) == 1:
        return prompts[0]
    return "I need these details to continue.\n" + "\n".join(f"- {prompt}" for prompt in prompts)


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
