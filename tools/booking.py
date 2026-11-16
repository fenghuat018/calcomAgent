"""Validate mutation plans, then claim and execute provider writes."""

from datetime import timedelta

from .sdk import local_tool
from .calcom_client import OPERATION_MARKER, ProviderFailure
from workflow_context import Booking, Read, Slot, Task, WorkflowContext, Write, iso

from scheduling_agents.inputs import EMAIL, event_for, interval, local_instants, remaining_day, resolve_input_times, zone_for
from scheduling_agents.outcomes import booking_label, finish
from scheduling_agents.state import ask, finish_write, logger, read_for, record_failure


def availability_query(ctx: WorkflowContext, event, duration: int) -> dict:
    task = ctx.task
    query = {"eventTypeId": event.id, "timeZone": task.time_zone}
    if event.supported_durations:
        query["duration"] = duration
    original = task.target_booking
    if task.intent == "reschedule_booking" and original and original.status != "cancelled":
        query["bookingUidToReschedule"] = original.calcom_uid
    return query


def alternatives(ctx: WorkflowContext, query: dict, *, purpose="alternatives") -> Read:
    task, request = ctx.task, ctx.task.inputs
    zone = zone_for(task.time_zone)
    bounds = interval(request.range_start, request.range_end, zone)
    if not bounds:
        bounds = remaining_day(task.selected_slot, zone)
    return Read("get_availability", purpose, query, bounds=bounds)


def prepare_booking(ctx: WorkflowContext):
    task, account = ctx.task, ctx.account
    resolve_input_times(task, ctx.clock())
    request = task.inputs
    original = task.target_booking
    cancellation = task.writes.get("cancel_booking")
    cancelled_by_task = cancellation and cancellation.state == "applied"

    if task.intent in ("cancel_booking", "reschedule_booking") and not cancelled_by_task:
        if task.fresh_revision != task.revision or original is None:
            return task.transfer(
                "retrieval", "prepare_retrieval", "Resolve and refresh the original booking.",
                task.save("target_request"),
            )
        if original.status == "cancelled":
            message = f"{booking_label(original, task.time_zone)} is already cancelled."
            status = "completed" if task.intent == "cancel_booking" else "failed"
            return finish(task, status, message, bookings=(original,), report_partial=False)
        if original.status not in ("accepted", "pending"):
            return finish(task, "failed", f"Cal.com shows this booking as {original.status}.")
        if task.intent == "cancel_booking":
            return plan_cancel(task, original)

    event = (
        account.event(original.event_type_id)
        if original and not request.event_type else event_for(account, request.event_type)
    )
    zone = zone_for(task.time_zone)
    attendee_zone = zone_for(request.time_zone or (original.timeZone if original else None)) or zone
    attendee_name = request.attendee_name or (original.name if original else None) or account.default_attendee_name
    attendee_email = request.attendee_email or (original.attendee_email if original else None) or account.default_attendee_email
    fields = dict(original.booking_fields) if original else {}
    fields.update({entry.field: entry.value for entry in request.booking_fields or ()})
    duration = request.duration
    if duration is None and original and event and event.id == original.event_type_id:
        duration = (original.slot.end - original.slot.start) // timedelta(minutes=1)
    if duration is None and event:
        duration = event.duration_minutes
    location = None
    if event:
        if request.location:
            location = next(
                (value for label, value in event.locations.items() if label.casefold() == request.location.casefold()),
                None,
            )
        elif original and event.id == original.event_type_id:
            location = original.location_input or event.locations.get(original.location)
        if location is None and not request.location and len(event.locations) == 1:
            location = next(iter(event.locations.values()))

    missing = []
    if not event:
        missing.append("eventTypeId")
    if not attendee_name:
        missing.append("attendee.name")
    if not attendee_email or not EMAIL.fullmatch(attendee_email):
        missing.append("attendee.email")
    if not zone or not attendee_zone:
        missing.append("attendee.timeZone")
    if event:
        if duration not in (event.supported_durations or (event.duration_minutes,)):
            missing.append("lengthInMinutes")
        missing.extend(
            f"bookingFieldsResponses.{key}" for key in event.required_fields
            if fields.get(key) is None or fields[key] == ""
        )
        if (request.location or event.location_required) and location is None:
            missing.append("location")
    if missing:
        return ask(task, "booking", "prepare_booking", fields=tuple(missing))

    bounds = interval(request.range_start, request.range_end, zone)
    if (request.range_start or request.range_end) and not bounds:
        return ask(task, "booking", "prepare_booking", fields=("range",))
    query = availability_query(ctx, event, duration)
    starts = local_instants(request.start, zone) if request.start else ()
    if len(starts) == 2:
        choices = tuple(Slot(start, start + timedelta(minutes=duration)) for start in starts)
        return ask(task, "booking", "prepare_booking", slots=choices, message=(
            "That local time occurs twice. Choose the first or second occurrence.\n"
            + "\n".join(
                f"{number}. {slot.start.astimezone(zone).isoformat()} [{zone.key}]"
                for number, slot in enumerate(choices, 1)
            )
        ))
    if not starts:
        task.selected_slot = None
        if bounds and not request.start:
            return read_for(task, Read("get_availability", "alternatives", query, bounds=bounds))
        return ask(task, "booking", "prepare_booking", fields=("start",))

    slot = Slot(starts[0], starts[0] + timedelta(minutes=duration))
    if bounds and (slot.start < bounds.start or slot.end > bounds.end):
        return ask(task, "booking", "prepare_booking", fields=("range",))
    if slot.start < ctx.clock() and (not original or slot != original.slot):
        return ask(task, "booking", "prepare_booking", fields=("start",), message="What future time would you like?")
    task.selected_slot = slot
    payload = {
        "start": iso(slot.start), "eventTypeId": event.id,
        "attendee": {"name": attendee_name, "email": attendee_email, "timeZone": attendee_zone.key},
    }
    if fields:
        payload["bookingFieldsResponses"] = fields
    if event.supported_durations:
        payload["lengthInMinutes"] = duration
    if location:
        payload["location"] = location
    task.replacement_plan = payload

    if (
        original and not cancelled_by_task and slot == original.slot and event.id == original.event_type_id
        and (attendee_name, attendee_email, attendee_zone.key) == (original.name, original.attendee_email, original.timeZone)
        and fields == original.booking_fields and not request.location
    ):
        return finish(task, "completed", f"The current booking already matches. {booking_label(original, task.time_zone)}",
                      bookings=(original,))

    if task.slot_preflight != (task.revision, slot):
        return read_for(task, Read("get_availability", "preflight", query, bounds=slot))
    if original and not cancelled_by_task:
        return plan_cancel(task, original)
    plan_write(task, "create_booking", payload)


def plan_cancel(task: Task, booking: Booking):
    body = {"cancelSubsequentBookings": False}
    if task.inputs.cancellation_reason:
        body["cancellationReason"] = task.inputs.cancellation_reason
    plan_write(task, "cancel_booking", body, booking.calcom_uid)


def plan_write(task: Task, kind, body: dict, uid: str | None = None):
    previous = task.writes.get(kind)
    if previous and previous.state in ("in_flight", "uncertain"):
        task.failed_kind, task.failed_write = "write", kind
        return finish(task, "recoverable", f"Cal.com has not yet verified operation {previous.id}.")
    if previous and previous.state == "applied":
        return finish_write(task, previous.result, kind)
    if previous and previous.body == body and previous.uid == uid:
        if previous.state != "planned":
            return finish(task, "recoverable", f"Operation {previous.id} needs a new validated choice or a permitted retry.")
        previous.transition("planned", revision=task.revision)
    else:
        operation = Write(kind, task.revision, body, uid)
        task.writes[kind] = operation
        task.operations[operation.id] = operation
    task.transition("booking", kind)


def check_write(ctx: WorkflowContext, kind: str) -> Write:
    task = ctx.task
    operation = task.writes.get(kind)
    if not operation or operation.state != "planned" or operation.revision != task.revision:
        raise ValueError("No current authorized write")
    if kind == "cancel_booking":
        original = task.target_booking
        if (
            task.intent not in ("cancel_booking", "reschedule_booking") or original is None
            or operation.uid != original.calcom_uid or not ctx.account.event(original.event_type_id)
            or task.fresh_revision != task.revision or original.status not in ("accepted", "pending")
        ):
            raise ValueError("Cancellation requires fresh, authorized booking evidence")
    if kind == "create_booking" or task.intent == "reschedule_booking":
        if (
            task.selected_slot is None or task.slot_preflight != (task.revision, task.selected_slot)
            or task.replacement_plan is None or not ctx.account.event(task.replacement_plan["eventTypeId"])
            or task.replacement_plan["start"] != iso(task.selected_slot.start)
        ):
            raise ValueError("The planned slot needs a fresh authorized availability check")
    if kind == "create_booking":
        if task.intent not in ("create_booking", "reschedule_booking") or operation.body != task.replacement_plan:
            raise ValueError("The saved create payload does not match the validated plan")
        if task.intent == "reschedule_booking":
            cancellation = task.writes.get("cancel_booking")
            if not cancellation or cancellation.state != "applied" or cancellation.result != task.target_booking:
                raise ValueError("The original cancellation must be verified first")
    return operation


def response_matches(ctx: WorkflowContext, operation: Write, booking: Booking) -> bool:
    if not ctx.account.event(booking.event_type_id):
        return False
    if operation.kind == "cancel_booking":
        original = operation.original
        return (
            original is not None and booking.calcom_uid == operation.uid and booking.booking_uid == original.booking_uid
            and booking.event_type_id == original.event_type_id and booking.slot == original.slot
            and booking.status == "cancelled" and not booking.rescheduled_to
        )
    return (
        booking.event_type_id == operation.body["eventTypeId"]
        and booking.slots["start"] == operation.body["start"]
        and booking.attendee_email.casefold() == operation.body["attendee"]["email"].casefold()
        and booking.status in ("accepted", "pending")
    )


async def execute_write(ctx: WorkflowContext, kind: str):
    task = ctx.task
    operation = check_write(ctx, kind)
    owner = ctx.write_claims.setdefault(operation.claim, operation.id)
    if owner != operation.id:
        if kind == "create_booking":
            return finish(task, "failed", "A create request for this attendee and time is already being verified.")
        task.interrupted_step = ("booking", "prepare_booking")
        return finish(task, "recoverable", "Another scheduling task is still verifying this booking.")
    if kind == "cancel_booking":
        operation.original = task.target_booking
    operation.transition("in_flight")
    logger.info("Cal.com write task=%s operation=%s step=%s state=%s attempt=%s",
                task.id, operation.id, kind, operation.state, operation.attempts)

    def verify(booking):
        return response_matches(ctx, operation, booking)

    try:
        if kind == "cancel_booking":
            result = await ctx.calcom.cancel_booking(operation.uid, operation.body, verify=verify)
        else:
            result = await ctx.calcom.create_booking({
                **operation.body, "metadata": {OPERATION_MARKER: operation.id},
            }, verify=verify)
    except ProviderFailure as error:
        operation.transition("uncertain" if error.error.category == "uncertain" else "unapplied")
        if operation.state == "unapplied":
            ctx.write_claims.pop(operation.claim, None)
        record_failure(ctx, error.error, operation)
        return
    operation.transition("applied", result=result)
    ctx.write_claims.pop(operation.claim, None)
    logger.info("Cal.com write task=%s operation=%s step=%s state=%s attempt=%s",
                task.id, operation.id, kind, operation.state, operation.attempts)
    finish_write(task, result, kind)


create_booking_tool = local_tool(
    "booking", "create_booking", lambda ctx: execute_write(ctx, "create_booking"),
    "Create the booking from its saved plan and slot.",
)
cancel_booking_tool = local_tool(
    "booking", "cancel_booking", lambda ctx: execute_write(ctx, "cancel_booking"),
    "Cancel the verified occurrence in the saved plan.",
)
