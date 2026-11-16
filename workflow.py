"""Conversation entry points for the Cal.com scheduling agents."""

import asyncio
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agents import Agent, Model, RunConfig, RunContextWrapper, RunHooks, Runner
from agents.items import TResponseInputItem

from tools.calcom_client import CalCom, UID, https_request
from scheduling_agents import create_agents
from scheduling_agents.inputs import source_contains, zone_for
from scheduling_agents.outcomes import finish
from workflow_context import Account, HandoffEvent, Outcome, Proposal, Role, Task, WorkflowContext


logger = logging.getLogger("calcom.scheduling")

NEW_REQUEST = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|i (?:want|need) to\s+)?"
    r"(?:book|schedule|set up|arrange|cancel|call off|reschedule|move|list|show|find|get|when|where)\b",
    re.I,
)
REFERENCE = re.compile(r"\b(?:option|one|that (?:booking|slot|time)|it|the (?:first|second|third|last))\b", re.I)


@dataclass
class MemorySession:
    session_id: str
    items: list[TResponseInputItem] = field(default_factory=list, repr=False)

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            return list(self.items)
        return self.items[-limit:] if limit > 0 else []

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self.items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        return self.items.pop() if self.items else None

    async def clear_session(self) -> None:
        self.items.clear()


@dataclass
class Conversation:
    context: WorkflowContext
    session: MemorySession
    turns: dict[str, Outcome] = field(default_factory=dict)
    most_recent: str | None = None


class ObserveHandoffs(RunHooks[WorkflowContext]):
    def __init__(self):
        self.events: list[HandoffEvent] = []

    async def on_handoff(
        self, context: RunContextWrapper[WorkflowContext],
        from_agent: Agent[WorkflowContext], to_agent: Agent[WorkflowContext],
    ) -> None:
        self.events.append(HandoffEvent(from_agent.name, to_agent.name, context.context.task.id))


class SchedulingWorkflow:
    """Keep one instance per authenticated Cal.com account for the process lifetime."""

    def __init__(
        self, account: Account, api_key: str, *, model: str | Model = "gpt-5-mini",
        transport=https_request, clock=lambda: datetime.now(timezone.utc),
        delay=asyncio.sleep, wait_budget: float = 5,
    ):
        self.account = account
        self.clock = clock
        self.delay = delay
        self.wait_budget = wait_budget
        self.client = CalCom(api_key, clock, transport)
        self.agents = create_agents(model)
        self._conversations: dict[str, Conversation] = {}
        self._write_claims: dict[tuple, str] = {}
        # Serializes both replies and writes targeting the same account, across conversations.
        self._lock = asyncio.Lock()

    async def close(self):
        await self.agents["triage"].model.close()

    def _conversation(self, conversation_id: str) -> Conversation:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            context = WorkflowContext(
                self.account, self.client, self.clock, self.delay, self.wait_budget,
                write_claims=self._write_claims,
            )
            conversation = Conversation(context, MemorySession(conversation_id))
            self._conversations[conversation_id] = conversation
        return conversation

    async def turn(
        self, message: str, conversation_id: str, *, reply_to: str | None = None,
        turn_id: str | None = None, question_key: str | None = None,
    ) -> Outcome:
        message = message.strip()
        if not message:
            raise ValueError("A scheduling message is required")
        async with self._lock:
            conversation = self._conversation(conversation_id)
            if turn_id and turn_id in conversation.turns:
                saved = conversation.turns[turn_id]
                return replace(saved, handoffs=())
            result = await self._turn(conversation, message, reply_to, question_key)
            if turn_id:
                conversation.turns[turn_id] = result
            return result

    async def _turn(
        self, conversation: Conversation, message: str, reply_to: str | None, question_key: str | None,
    ):
        ctx = conversation.context
        pending = [task for task in ctx.tasks.values() if task.status == "awaiting_input"]
        linked = [
            task for task in pending
            if source_contains(message, task.id)
            or any(source_contains(message, item.calcom_uid) for item in task.pending.bookings)
        ]
        if question_key:
            task = next((
                candidate for candidate in pending
                if candidate.pending.key == question_key and (not reply_to or candidate.id == reply_to)
            ), None)
            if not task:
                return Outcome(reply_to or uuid4().hex, "not_found", "That scheduling question is no longer current.")
        elif reply_to:
            task = ctx.tasks.get(reply_to)
            if not task or task.status != "awaiting_input":
                return Outcome(reply_to, "not_found", "No open scheduling question has that task reference.")
        elif len(linked) == 1:
            task = linked[0]
        elif len(linked) > 1 or pending and not (
            NEW_REQUEST.match(message) and not REFERENCE.search(message)
            and not (len(pending) == 1 and pending[0].pending.fields == ("intent",))
        ):
            if len(pending) == 1:
                task = pending[0]
            else:
                options = "\n".join(f"{item.id} — {item.pending.message.splitlines()[0]}" for item in pending)
                return Outcome(uuid4().hex, "awaiting_input", f"Which task are you answering? Include its reference.\n{options}")
        else:
            task = None

        ctx.referenced_uid = None
        ctx.referenced_availability = None
        if task is None:
            previous = ctx.tasks.get(conversation.most_recent)
            if previous and previous.outcome and previous.outcome.status == "completed" and REFERENCE.search(message):
                entries = previous.outcome.bookings
                if len(entries) == 1:
                    ctx.referenced_uid = entries[0].calcom_uid
                if previous.intent == "get_availability" and previous.read and previous.available_slots:
                    ctx.referenced_availability = (
                        previous.read.query["eventTypeId"], previous.time_zone, previous.available_slots,
                    )
            if previous and previous.request == message and previous.intent in (
                "create_booking", "cancel_booking", "reschedule_booking",
            ) and previous.status in ("completed", "recoverable"):
                return replace(previous.outcome, handoffs=())
            task = Task(uuid4().hex, message, message, received_at=self.clock())
            ctx.tasks[task.id] = task
            role = "triage"
        else:
            task.message = message
            task.transition("confirmation", "save_answer")
            role = "confirmation"
        return await self._run(conversation, task, role, message)

    async def resume(self, task_id: str, conversation_id: str, *, booking_reference: str | None = None) -> Outcome:
        async with self._lock:
            conversation = self._conversation(conversation_id)
            task = conversation.context.tasks.get(task_id)
            if task is None:
                if booking_reference and UID.fullmatch(booking_reference):
                    return await self._lookup_old_booking(conversation, booking_reference)
                return Outcome(
                    task_id, "not_found",
                    "This process has no matching task. Provide its booking UID or numeric ID for a fresh lookup.",
                )
            if task.status not in ("recoverable", "incomplete"):
                return replace(task.outcome, handoffs=())
            operation = task.failed_operation
            if operation and operation.retry_at and self.clock() < operation.retry_at:
                return replace(
                    task.outcome, message=f"{task.outcome.message}\nEarliest retry {operation.retry_at.isoformat()}.",
                    handoffs=(),
                )
            if task.failed_kind == "read" and task.read and task.read.attempts >= 3:
                task.read.attempts = 0
                task.read.generation += 1
            task.transition("recovery", "record_recovery")
            return await self._run(conversation, task, "recovery", f"Resume scheduling task {task_id}.")

    async def _lookup_old_booking(self, conversation: Conversation, reference: str) -> Outcome:
        ctx = conversation.context
        message = f"Look up booking {reference} after a restart."
        numeric_id = int(reference) if reference.isdigit() else None
        uid = None if numeric_id is not None else reference
        task = Task(
            uuid4().hex, message, message, received_at=self.clock(), intent="get_booking",
            role="retrieval", step="prepare_retrieval", reference_uid=uid,
            inputs=Proposal(intent="get_booking", uid=uid, numeric_id=numeric_id),
            time_zone=ctx.saved_time_zone or ctx.account.default_time_zone,
        )
        ctx.tasks[task.id] = task
        return await self._run(conversation, task, "retrieval", message)

    async def _run(self, conversation: Conversation, task: Task, role: Role, message: str) -> Outcome:
        ctx = conversation.context
        ctx.active_task_id = task.id
        ctx.deadline = self.clock() + timedelta(seconds=self.wait_budget)
        hooks = ObserveHandoffs()
        try:
            await Runner.run(
                self.agents[role], message, context=ctx, session=conversation.session, hooks=hooks,
                max_turns=60, run_config=RunConfig(
                    tracing_disabled=True, trace_include_sensitive_data=False, workflow_name="Cal.com scheduling",
                ),
            )
            if task.outcome is None:
                self._interrupted(task, "missing_outcome")
        except Exception as error:
            logger.warning("Scheduling run interrupted task=%s role=%s step=%s reason=%s",
                           task.id, task.role, task.step, type(error).__name__)
            if task.outcome is None:
                self._interrupted(task, type(error).__name__)
        except asyncio.CancelledError:
            if task.outcome is None:
                self._interrupted(task, "cancelled_run")
            raise
        finally:
            ctx.active_task_id = None
            ctx.referenced_uid = None
            ctx.referenced_availability = None
        if zone_for(task.time_zone):
            ctx.saved_time_zone = task.time_zone
        conversation.most_recent = task.id
        return replace(task.outcome, handoffs=tuple(hooks.events))

    def _interrupted(self, task: Task, failure: str):
        task.run_failure = failure
        uncertain = next((
            write for write in task.writes.values() if write.state in ("in_flight", "uncertain")
        ), None)
        if uncertain:
            task.failed_kind, task.failed_write = "write", uncertain.kind
        elif task.route:
            task.interrupted_step = task.route.destination, task.route.next_step
        else:
            task.interrupted_step = task.role, task.step
        finish(task, "recoverable", f"Scheduling paused. Resume task {task.id} from its saved checkpoint.")
