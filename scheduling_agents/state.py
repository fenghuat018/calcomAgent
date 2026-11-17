"""Checked state updates shared by the specialists."""

import logging
from datetime import datetime, timedelta

from workflow_context import APIError, Booking, Evidence, Question, Read, Role, Slot, Task, WorkflowContext, Write

from .outcomes import booking_label, clarification_prompt, finish, interval_label


logger = logging.getLogger("calcom.scheduling")


def ask(
    task: Task, destination: Role, step: str, *, fields: tuple[str, ...] = (),
    bookings: tuple[Booking, ...] = (), slots: tuple[Slot, ...] = (),
    message: str | None = None, clears_slot: bool = False, reconciliation_notice: bool = False,
    event_names: tuple[str, ...] = (),
):
    if message is None:
        if bookings:
            message = "Which booking do you mean? Reply with its option number, such as “Option 1”.\n" + "\n".join(
                f"{index}. {booking_label(booking, task.time_zone)}"
                for index, booking in enumerate(bookings, 1)
            )
        elif slots:
            message = "Which time would you like? Reply with its option number, such as “Option 1”.\n" + "\n".join(
                f"{index}. {interval_label(slot, task.time_zone)}"
                for index, slot in enumerate(slots, 1)
            )
        else:
            message = clarification_prompt(fields, event_names)
    key = task.save("question")
    task.pending = Question(
        key, task.revision, message, destination, step, fields, bookings, slots, clears_slot,
        reconciliation_notice,
    )
    task.verified[key] = Evidence("question", task.revision, task.pending)
    task.transfer("confirmation", "save_question", "A user answer is required.", key, question_key=key)


def read_for(task: Task, read: Read):
    task.read = read
    if task.role == "retrieval":
        task.transition("retrieval", read.kind)
    else:
        task.transfer("retrieval", read.kind, f"Run the saved {read.purpose} read.", task.save("read", read))


def record_failure(ctx: WorkflowContext, error: APIError, operation: Read | Write, *, page_key: str = ""):
    task = ctx.task
    is_write = isinstance(operation, Write)
    operation_id = operation.id if is_write else operation.key
    key = f"{task.revision}:{operation_id}:{page_key}:{operation.attempts}"
    task.api_errors[key] = error
    task.failure_key = key
    task.failed_kind = "write" if is_write else "read"
    operation.retry_at = None
    if is_write:
        task.failed_write = operation.kind
        operation.failure_key = key
    if error.retry_after:
        now = ctx.clock()
        try:
            operation.retry_at = now + timedelta(seconds=error.retry_after)
        except OverflowError:
            operation.retry_at = datetime.max.replace(tzinfo=now.tzinfo)
    logger.info(
        "Cal.com operation failed task=%s operation=%s step=%s category=%s status=%s attempt=%s",
        task.id, operation_id, task.step, error.category, error.error_code, operation.attempts,
    )
    task.transfer("recovery", "record_recovery", "Cal.com returned a recorded failure.", key)
    return key


def finish_write(task: Task, booking: Booking, kind: str):
    if kind == "cancel_booking":
        task.target_booking = booking
        if task.intent == "reschedule_booking":
            task.slot_preflight = None
            task.transition("booking", "prepare_booking")
        else:
            finish(task, "completed", f"Cancelled {booking_label(booking, task.time_zone)}.", bookings=(booking,))
        return
    if task.intent == "reschedule_booking":
        task.replacement = booking
        original = interval_label(task.target_booking.slot, task.time_zone or task.target_booking.timeZone)
        message = f"Rescheduled from {original} to {booking_label(booking, task.time_zone)}."
    else:
        task.target_booking = booking
        message = f"Booked {booking_label(booking, task.time_zone)}."
    finish(task, "completed", message, bookings=(booking,))
