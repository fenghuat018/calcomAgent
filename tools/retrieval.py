"""Authorized booking resolution, pagination, detail and availability reads."""

from .sdk import local_tool
from .calcom_client import ProviderFailure, UID
from workflow_context import Account, Booking, Read, Slot, Task, WorkflowContext

from scheduling_agents.inputs import event_for, interval, local_instants, resolve_input_times, text_matches, zone_for
from scheduling_agents.outcomes import booking_label, finish, interval_label
from scheduling_agents.state import ask, read_for, record_failure


def event_query(account: Account, event) -> dict:
    if event:
        return {"eventTypeId": event.id}
    return {"eventTypeIds": ",".join(str(item.id) for item in account.event_types)}


def prepare_retrieval(ctx: WorkflowContext):
    task, account = ctx.task, ctx.account
    resolve_input_times(task, ctx.clock())
    request = task.inputs
    if task.intent == "get_availability":
        event = event_for(account, request.event_type)
        zone = zone_for(task.time_zone)
        bounds = interval(request.range_start, request.range_end, zone)
        missing = []
        if not event:
            missing.append("eventTypeId")
        if not zone:
            missing.append("timeZone")
        elif not bounds:
            missing.append("range")
        if event and request.duration is not None and request.duration not in (
            event.supported_durations or (event.duration_minutes,)
        ):
            missing.append("lengthInMinutes")
        if missing:
            return ask(task, "retrieval", "prepare_retrieval", fields=tuple(missing))
        query = {"eventTypeId": event.id, "timeZone": zone.key}
        if event.supported_durations and request.duration:
            query["duration"] = request.duration
        return read_for(task, Read("get_availability", "availability", query, bounds=bounds))

    if task.intent == "list_bookings":
        zone = zone_for(task.time_zone)
        bounds = interval(request.range_start, request.range_end, zone)
        event = event_for(account, request.event_type)
        missing = []
        if not zone:
            missing.append("timeZone")
        elif not bounds:
            missing.append("range")
        if request.event_type and not event:
            missing.append("eventTypeId")
        if missing:
            return ask(task, "retrieval", "prepare_retrieval", fields=tuple(missing))
        query = event_query(account, event)
        if request.attendee_email:
            query["attendeeEmail"] = request.attendee_email
        return read_for(task, Read("list_bookings", "listing", query, bounds=bounds))

    destination = "retrieval" if task.intent == "get_booking" else "booking"
    continuation = "prepare_retrieval" if destination == "retrieval" else "prepare_booking"
    if task.time_zone and not zone_for(task.time_zone):
        return ask(task, destination, continuation, fields=("timeZone",))
    uid = task.selected_booking.calcom_uid if task.selected_booking else task.reference_uid
    if uid and UID.fullmatch(uid):
        return read_for(task, Read("get_booking", "target detail", uid=uid))
    if not any((
        request.numeric_id, request.attendee_email, request.attendee_clue,
        request.title_clue, request.target_start,
    )):
        return ask(task, destination, continuation, fields=("bookingReference",))
    if request.event_type and task.intent != "reschedule_booking" and not event_for(account, request.event_type):
        return ask(task, destination, continuation, fields=("eventTypeId",))
    if request.target_start:
        zone = zone_for(task.time_zone)
        if not zone:
            return ask(task, destination, continuation, fields=("timeZone",))
        if len(local_instants(request.target_start, zone, allow_date=True)) != 1 or (
            request.target_end and not interval(request.target_start, request.target_end, zone)
        ):
            return ask(task, destination, continuation, fields=("bookingReference",))
    event = event_for(account, None if task.intent == "reschedule_booking" else request.event_type)
    query = event_query(account, event)
    if request.attendee_email and task.intent != "reschedule_booking":
        query["attendeeEmail"] = request.attendee_email
    return read_for(task, Read("list_bookings", "target search", query))


def in_scope(ctx: WorkflowContext, booking: Booking, read: Read) -> bool:
    event_id = read.query.get("eventTypeId")
    email = read.query.get("attendeeEmail")
    return bool(
        ctx.account.event(booking.event_type_id)
        and (event_id is None or event_id == booking.event_type_id)
        and (email is None or email.casefold() == booking.attendee_email.casefold())
    )


def overlaps(slot: Slot, bounds: Slot) -> bool:
    return slot.start < bounds.end and slot.end > bounds.start


def listing(task: Task, read: Read) -> tuple[Booking, ...]:
    statuses = task.inputs.statuses or ("accepted", "pending")
    clue = task.inputs.attendee_clue
    title = task.inputs.title_clue
    return tuple(
        booking for booking in sorted(read.rows.values(), key=lambda row: (row.slot.start, row.calcom_uid))
        if booking.status in statuses and overlaps(booking.slot, read.bounds)
        and (not clue or text_matches(clue, booking.name))
        and (not title or text_matches(title, booking.title))
    )


def matches_target(task: Task, booking: Booking) -> bool:
    request = task.inputs
    if request.numeric_id is not None and request.numeric_id != booking.booking_uid:
        return False
    if request.attendee_clue and not text_matches(request.attendee_clue, booking.name):
        return False
    if request.title_clue and not text_matches(request.title_clue, booking.title):
        return False
    if request.attendee_email and task.intent != "reschedule_booking":
        if request.attendee_email.casefold() != booking.attendee_email.casefold():
            return False
    if request.target_start:
        zone = zone_for(task.time_zone)
        if request.target_end:
            bounds = interval(request.target_start, request.target_end, zone)
            return bool(bounds and overlaps(booking.slot, bounds))
        starts = local_instants(request.target_start, zone, allow_date=True)
        return len(starts) == 1 and starts[0] == booking.slot.start
    return True


def not_found(task: Task):
    reference = "the specified reference" if task.reference_uid or task.inputs.numeric_id else "the supplied clues"
    finish(task, "not_found", f"No booking in the authorized event types matched {reference}. Supply another reference to search again.")


def complete_list(ctx: WorkflowContext, read: Read):
    task = ctx.task
    if read.purpose == "listing":
        rows = listing(task, read)
        window = interval_label(read.bounds, task.time_zone)
        message = "\n".join(booking_label(row, task.time_zone) for row in rows)
        return finish(task, "completed", message or f"No bookings found for {window}.", bookings=rows)
    if read.purpose == "reconcile create":
        operation = task.writes[task.reconcile_write]
        task.recovery_result = "reconciliation"
        matches = tuple(
            booking for booking in read.rows.values()
            if booking.operation_marker == operation.id and booking.status in ("accepted", "pending")
            and booking.slot.start == read.bounds.start and booking.slot.end == read.bounds.end
        )
        task.transfer(
            "recovery", "record_recovery", "The scoped reconciliation search finished.",
            task.save("reconciliation", matches), continuation=True,
        )
        task.reconciled_booking = matches[0] if len(matches) == 1 else None
        return

    rows = tuple(sorted(
        (row for row in read.rows.values() if matches_target(task, row)),
        key=lambda row: (row.slot.start, row.calcom_uid),
    ))
    if task.intent in ("cancel_booking", "reschedule_booking"):
        active = tuple(row for row in rows if row.status in ("accepted", "pending"))
        rows = active or tuple(row for row in rows if row.status == "cancelled")
    if not rows:
        return not_found(task)
    request = task.inputs
    weak = len(rows) == 1 and not any((
        request.numeric_id, request.attendee_email,
        request.target_start and (request.attendee_clue or request.title_clue or not request.target_end),
        request.attendee_clue and request.attendee_clue.casefold() == rows[0].name.casefold(),
        request.title_clue and request.title_clue.casefold() == rows[0].title.casefold(),
    ))
    if len(rows) > 1 or weak:
        destination = "retrieval" if task.intent == "get_booking" else "booking"
        step = "prepare_retrieval" if destination == "retrieval" else "prepare_booking"
        return ask(task, destination, step, bookings=rows)
    task.selected_booking = rows[0]
    return read_for(task, Read("get_booking", "target detail", uid=rows[0].calcom_uid))


async def list_bookings(ctx: WorkflowContext):
    task = ctx.task
    read = task.read
    while True:
        read.attempts += 1
        try:
            rows, cursor = await ctx.calcom.list_bookings(read.query, read.cursor, read.seen_cursors)
        except ProviderFailure as error:
            return record_failure(
                ctx, error.error, read,
                page_key=f"{read.generation}-{len(read.seen_cursors)}",
            )
        for booking in rows:
            if in_scope(ctx, booking, read):
                read.rows[booking.calcom_uid] = booking
        read.attempts = 0
        read.retry_at = None
        read.cursor = cursor
        if cursor is None:
            return complete_list(ctx, read)
        read.seen_cursors.add(cursor)


def complete_detail(ctx: WorkflowContext, booking: Booking, read: Read):
    task = ctx.task
    if read.purpose == "reconcile cancel":
        task.reconciled_booking = booking
        task.recovery_result = "reconciliation"
        return task.transfer(
            "recovery", "record_recovery", "The cancellation read-back finished.",
            task.save("reconciliation", booking), continuation=True,
        )
    observed = task.selected_booking or task.target_booking
    if not observed and not matches_target(task, booking):
        return not_found(task)
    if observed and observed.material_details() != booking.material_details():
        destination = "retrieval" if task.intent == "get_booking" else "booking"
        step = "prepare_retrieval" if destination == "retrieval" else "prepare_booking"
        return ask(task, destination, step, bookings=(booking,), message=(
            "Cal.com shows changed booking details. Confirm whether to use this booking.\n"
            f"1. {booking_label(booking, task.time_zone)}"
        ))
    task.selected_booking = booking
    task.target_booking = booking
    task.fresh_revision = task.revision
    task.time_zone = task.time_zone or (booking.timeZone if zone_for(booking.timeZone) else "UTC")
    if task.intent == "get_booking":
        details = [booking_label(booking, task.time_zone), f"Event type {booking.event_type_id}"]
        if booking.name:
            details.append(f"Attendee {booking.name}")
        if booking.host_name:
            details.append(f"Host {booking.host_name}")
        if booking.meeting_url or booking.location:
            details.append(booking.meeting_url or booking.location)
        return finish(task, "completed", "\n".join(details), bookings=(booking,))
    return task.transfer(
        "booking", "prepare_booking", "The booking identity and current state were verified.",
        task.save("booking_detail", booking), continuation=True,
    )


async def get_booking(ctx: WorkflowContext):
    task = ctx.task
    read = task.read
    read.attempts += 1
    try:
        booking = await ctx.calcom.get_booking(read.uid)
    except ProviderFailure as error:
        return record_failure(ctx, error.error, read, page_key=str(read.generation))
    if booking.calcom_uid != read.uid or not in_scope(ctx, booking, read):
        if read.purpose == "reconcile cancel":
            task.recovery_result = "reconciliation"
            task.reconciled_booking = None
            task.transfer("recovery", "record_recovery", "Read-back provided no authorized match.",
                          task.save("reconciliation"), continuation=True)
        else:
            not_found(task)
        return
    complete_detail(ctx, booking, read)


async def get_availability(ctx: WorkflowContext):
    task = ctx.task
    read = task.read
    read.attempts += 1
    try:
        slots = await ctx.calcom.get_availability(read.query, read.bounds)
    except ProviderFailure as error:
        return record_failure(ctx, error.error, read, page_key=str(read.generation))

    task.available_slots = slots
    if read.purpose == "availability":
        label = interval_label(read.bounds, task.time_zone)
        lines = "\n".join(
            f"{index}. {interval_label(slot, task.time_zone)}" for index, slot in enumerate(slots, 1)
        )
        return finish(task, "completed", lines or f"No slots are available in {label}.", slots=slots)
    if read.purpose == "preflight" and task.selected_slot in slots:
        task.slot_preflight = (task.revision, task.selected_slot)
        return task.transfer(
            "booking", "prepare_booking", "The selected slot is currently available.",
            task.save("availability_result", task.selected_slot), continuation=True,
        )

    if read.purpose == "preflight" and task.selected_slot:
        # Exact requests first check the selected interval, then query fresh alternatives.
        from .booking import alternatives
        return read_for(task, alternatives(ctx, read.query))
    if read.purpose == "rejected slot":
        task.available_slots = tuple(slot for slot in slots if slot != task.selected_slot)
        task.recovery_result = "alternatives"
        return task.transfer(
            "recovery", "record_recovery", "Fresh alternatives are ready for review.",
            task.save("alternatives", task.available_slots), continuation=True,
        )
    offer_slots(task, read.bounds)


def offer_slots(task: Task, bounds: Slot):
    scope = interval_label(bounds, task.time_zone)
    available = task.available_slots
    if available:
        lines = "\n".join(
            f"{number}. {interval_label(slot, task.time_zone)}"
            for number, slot in enumerate(available, 1)
        )
        return ask(task, "booking", "prepare_booking", slots=available, message=(
            f"Which available time would you like in {scope}?\n{lines}"
        ), clears_slot=True)
    return ask(task, "booking", "prepare_booking", fields=("range",), message=(
        f"No alternative slots are available in {scope}. What other time window should I check?"
    ), clears_slot=True)


list_bookings_tool = local_tool(
    "retrieval", "list_bookings", list_bookings, "Read the complete scoped booking list.",
)
get_booking_tool = local_tool(
    "retrieval", "get_booking", get_booking, "Read and save the current booking details.",
)
get_availability_tool = local_tool(
    "retrieval", "get_availability", get_availability, "Read and save slots for the validated range.",
)
