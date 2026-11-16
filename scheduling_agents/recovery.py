"""Recover from recorded provider evidence with a bounded retry budget."""

from dataclasses import replace
from datetime import timedelta
from random import uniform

from workflow_context import Read, Slot, WorkflowContext, Write, instant

from tools.booking import alternatives, availability_query, response_matches
from .inputs import zone_for
from .outcomes import booking_label, finish
from tools.retrieval import listing, not_found, offer_slots
from .state import ask, finish_write, read_for


MAX_ATTEMPTS = 3


async def within_retry_budget(ctx: WorkflowContext, operation: Read | Write) -> bool:
    attempts = operation.attempts
    if attempts >= MAX_ATTEMPTS:
        return False
    now = ctx.clock()
    if operation.retry_at is None:
        operation.retry_at = now + timedelta(seconds=0.25 * 2 ** (max(1, attempts) - 1) * uniform(0.8, 1.2))
    wait = max(0, (operation.retry_at - now).total_seconds())
    if now + timedelta(seconds=wait) > ctx.deadline:
        return False
    await ctx.delay(wait)
    operation.retry_at = None
    return True


async def record_recovery(ctx: WorkflowContext):
    task = ctx.task
    if task.recovery_result == "reconciliation":
        task.recovery_result = None
        return reconcile_result(ctx)
    if task.recovery_result == "alternatives":
        task.recovery_result = None
        return offer_slots(task, task.read.bounds)

    if task.interrupted_step is not None:
        role, step = task.interrupted_step
        task.interrupted_step = None
        if role == "triage":
            return ask(task, "triage", "save_intent", fields=("intent",))
        if role == "recovery":
            pass
        elif role == "booking":
            return resume_booking(task, "Resume the saved booking plan after revalidation.")
        else:
            question = task.pending if role == "confirmation" else None
            return task.transfer(role, step, "Resume from the saved workflow checkpoint.",
                                 task.save("checkpoint", (role, step)),
                                 question_key=question.key if question else None, continuation=True)

    if task.failed_kind == "read":
        return await recover_read(ctx)
    if task.failed_kind == "write":
        return await recover_write(ctx)
    finish(task, "recoverable", f"Scheduling task {task.id} is waiting for a continuation.")


async def recover_read(ctx: WorkflowContext):
    task, read = ctx.task, ctx.task.read
    error = task.api_errors[task.failure_key]
    if error.category == "temporary" and await within_retry_budget(ctx, read):
        return task.transfer("retrieval", read.kind, "Retry the recorded read at its saved position.",
                             task.failure_key, continuation=True)
    if read.purpose == "listing":
        rows = listing(task, read)
        status = "incomplete" if read.rows or read.seen_cursors else (
            "recoverable" if error.category == "temporary" else "failed"
        )
        lines = "\n".join(booking_label(row, task.time_zone) for row in rows)
        message = f"{error.error_message} The search could not finish (task {task.id})."
        return finish(task, status, f"{message}\n{lines}" if lines else message, bookings=rows)
    if read.purpose.startswith("reconcile "):
        return unresolved(ctx)
    if error.category == "missing" and read.purpose == "target detail":
        return not_found(task)
    status = "recoverable" if error.category == "temporary" else "failed"
    finish(task, status, f"{error.error_message} The read could not finish (task {task.id}).")


def resume_booking(task, reason: str):
    task.fresh_revision = None
    task.slot_preflight = None
    task.transfer("booking", "prepare_booking", reason, task.save("checkpoint"), continuation=True)


async def recover_write(ctx: WorkflowContext):
    task = ctx.task
    operation = task.writes[task.failed_write]
    if operation.state in ("uncertain", "in_flight"):
        if operation.state == "in_flight":
            operation.transition("uncertain")
        task.reconcile_write = operation.kind
        task.reconciled_booking = None
        if operation.kind == "cancel_booking":
            return read_for(task, Read("get_booking", "reconcile cancel", uid=operation.uid))
        body = operation.body
        start = instant(body["start"])
        event = ctx.account.event(body["eventTypeId"])
        duration = body.get("lengthInMinutes", event.duration_minutes)
        query = {"eventTypeId": event.id, "attendeeEmail": body["attendee"]["email"]}
        bounds = Slot(start, start + timedelta(minutes=duration))
        return read_for(task, Read("list_bookings", "reconcile create", query, bounds=bounds))

    error = task.api_errors[operation.failure_key]
    if error.category == "slot_conflict" and operation.kind == "create_booking":
        body = operation.body
        event = ctx.account.event(body["eventTypeId"])
        query = availability_query(ctx, event, body.get("lengthInMinutes", event.duration_minutes))
        return read_for(task, alternatives(ctx, query, purpose="rejected slot"))
    if error.category == "temporary":
        if await within_retry_budget(ctx, operation):
            operation.transition("planned")
            return resume_booking(task, "Retry the definitively unapplied write after revalidation.")
        if operation.retry_at and operation.attempts < MAX_ATTEMPTS:
            return finish(task, "recoverable", f"{error.error_message} Resume task {task.id} after the retry delay.")
    if error.category == "invalid_input" and error.field:
        fields = (error.field,)
        return ask(task, "booking", "prepare_booking", fields=fields)
    if error.category == "missing" and operation.kind == "cancel_booking":
        task.reference_uid = None
        task.selected_booking = None
        task.target_booking = None
        return ask(task, "booking", "prepare_booking", fields=("bookingReference",), message=(
            "Cal.com could not find that booking for cancellation. Which booking should I check?"
        ))
    finish(task, "failed", f"{error.error_message} Request reference {operation.id}.")


def unresolved(ctx: WorkflowContext, detail: str = ""):
    task = ctx.task
    operation = task.writes[task.reconcile_write]
    summary = f"Cal.com has not confirmed the outcome of operation {operation.id}. Resume task {task.id} to check again."
    finish(task, "recoverable", f"{detail} {summary}".strip())


def reconcile_result(ctx: WorkflowContext):
    task = ctx.task
    operation = task.writes[task.reconcile_write]
    result = task.reconciled_booking
    task.reconciled_booking = None
    original = operation.original
    if result and original and operation.kind == "cancel_booking":
        observed = replace(result, status=original.status) if result.status == "cancelled" else result
        if observed.material_details() != original.material_details():
            detail = f"Cal.com shows changed booking details. {booking_label(result, task.time_zone)}"
            task.failed_kind, task.failed_write = "write", operation.kind
            return ask(task, "booking", "prepare_booking", bookings=(result,), reconciliation_notice=True, message=(
                f"{detail}\nCancellation remains unverified. Resume task {task.id} to check again."
            ))
    if not result or not response_matches(ctx, operation, result):
        task.failed_kind, task.failed_write = "write", operation.kind
        return unresolved(ctx)
    operation.transition("applied", result=result)
    task.reconcile_write = None
    ctx.write_claims.pop(operation.claim, None)
    if operation.kind == "cancel_booking" and task.intent == "reschedule_booking":
        task.target_booking = result
        return resume_booking(task, "Cancellation was verified. Recheck the replacement slot.")
    finish_write(task, result, operation.kind)
