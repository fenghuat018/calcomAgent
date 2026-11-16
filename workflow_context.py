"""Application-owned state for a single authenticated scheduling conversation."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from tools.calcom_client import CalCom


Role = Literal["triage", "booking", "retrieval", "confirmation", "recovery"]
Intent = Literal[
    "create_booking", "cancel_booking", "reschedule_booking",
    "list_bookings", "get_booking", "get_availability", "unclear",
]
Status = Literal[
    "active", "awaiting_input", "completed", "not_found", "incomplete",
    "recoverable", "failed",
]
WriteState = Literal["planned", "in_flight", "applied", "unapplied", "uncertain"]
WRITE_TRANSITIONS = {
    "planned": {"in_flight"},
    "in_flight": {"uncertain", "applied", "unapplied"},
    "uncertain": {"applied"},
    "unapplied": {"planned"},
    "applied": set(),
}
WRITE_INTENTS = frozenset({"create_booking", "cancel_booking", "reschedule_booking"})
READ_INTENTS = frozenset({"list_bookings", "get_booking", "get_availability"})


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Expected a timestamp with a UTC offset")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True, order=True)
class Slot:
    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("Slot endpoints require UTC offsets")
        # A repeated local hour has different instants even when both values share a zone.
        if self.start.timestamp() >= self.end.timestamp():
            raise ValueError("The interval must have positive length")

    def local(self, zone: ZoneInfo) -> "Slot":
        return Slot(self.start.astimezone(zone), self.end.astimezone(zone))


@dataclass(frozen=True)
class Booking:
    booking_uid: int
    calcom_uid: str
    name: str
    timeZone: str
    slots: dict[str, str]
    event_type_id: int
    status: str
    title: str
    attendee_email: str = ""
    host_name: str = ""
    location: str = ""
    meeting_url: str = ""
    booking_fields: dict = field(default_factory=dict)
    operation_marker: str | None = None
    rescheduled_to: str | None = None
    location_input: dict | None = None

    @property
    def slot(self) -> Slot:
        return Slot(instant(self.slots["start"]), instant(self.slots["end"]))

    def material_details(self) -> tuple:
        return (
            self.booking_uid, self.calcom_uid, self.slots, self.event_type_id,
            self.status, self.name, self.attendee_email, self.timeZone,
            self.booking_fields, self.location, self.rescheduled_to,
        )


@dataclass(frozen=True)
class EventType:
    id: int
    name: str
    duration_minutes: int
    required_fields: tuple[str, ...] = ()
    supported_durations: tuple[int, ...] = ()
    location_required: bool = False
    locations: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class Account:
    event_types: tuple[EventType, ...]
    default_time_zone: str | None = None
    default_attendee_name: str | None = None
    default_attendee_email: str | None = None

    def __post_init__(self):
        if not self.event_types or len({item.id for item in self.event_types}) != len(self.event_types):
            raise ValueError("Configure one or more distinct Cal.com event types")
        if any(
            type(event.id) is not int or event.id <= 0
            or not isinstance(event.name, str) or not event.name.strip()
            or type(event.duration_minutes) is not int or event.duration_minutes <= 0
            or any(type(duration) is not int or duration <= 0 for duration in event.supported_durations)
            or (event.supported_durations and event.duration_minutes not in event.supported_durations)
            or any(not isinstance(key, str) or not key.strip() for key in event.required_fields)
            or not isinstance(event.locations, dict)
            or any(not isinstance(value, dict) for value in event.locations.values())
            for event in self.event_types
        ):
            raise ValueError("Invalid Cal.com event type configuration")
        if self.default_time_zone:
            ZoneInfo(self.default_time_zone)

    def event(self, event_id: int) -> EventType | None:
        return next((event for event in self.event_types if event.id == event_id), None)


@dataclass(frozen=True)
class APIError:
    request_method: str
    request_url: str
    request_body: dict
    error_code: int
    error_message: str
    category: str
    retry_after: float = 0
    field: str | None = None


class Proposal(BaseModel):
    """Untrusted interpretation of an utterance. Application code validates it."""

    model_config = ConfigDict(extra="forbid")

    intent: Intent = "unclear"
    event_type: str | None = None
    attendee_name: str | None = None
    attendee_email: str | None = None
    time_zone: str | None = None
    uid: str | None = None
    numeric_id: int | None = None
    attendee_clue: str | None = None
    title_clue: str | None = None
    target_start: str | None = None
    target_end: str | None = None
    start: str | None = None
    range_start: str | None = None
    range_end: str | None = None
    duration: int | None = None
    statuses: list[Literal["accepted", "pending", "cancelled", "rejected"]] | None = None
    booking_fields: list["FieldValue"] | None = None
    location: str | None = None
    cancellation_reason: str | None = None
    choice_from_availability: int | None = None


class FieldValue(BaseModel):
    field: str
    value: str


Proposal.model_rebuild()


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: Proposal | None = None
    choice: int | None = None


@dataclass(frozen=True)
class Question:
    key: str
    revision: int
    message: str
    destination: Role
    step: str
    fields: tuple[str, ...] = ()
    bookings: tuple[Booking, ...] = ()
    slots: tuple[Slot, ...] = ()
    clears_slot: bool = False
    reconciliation_notice: bool = False


@dataclass(frozen=True)
class HandoffEvent:
    source: Role
    target: Role
    task_id: str


@dataclass(frozen=True)
class Outcome:
    task_id: str
    status: Status
    message: str
    time_zone: str | None = None
    bookings: tuple[Booking, ...] = ()
    slots: tuple[Slot, ...] = ()
    candidates: tuple[Booking, ...] = ()
    missing_fields: tuple[str, ...] = ()
    original: Booking | None = None
    replacement: Booking | None = None
    handoffs: tuple[HandoffEvent, ...] = ()
    question_key: str | None = None


@dataclass
class Read:
    kind: Literal["list_bookings", "get_booking", "get_availability"]
    purpose: str
    query: dict = field(default_factory=dict)
    uid: str | None = None
    bounds: Slot | None = None
    cursor: str | None = None
    seen_cursors: set[str] = field(default_factory=set)
    rows: dict[str, Booking] = field(default_factory=dict)
    attempts: int = 0
    generation: int = 0
    key: str = field(default_factory=lambda: uuid4().hex)
    retry_at: datetime | None = None


@dataclass
class Write:
    kind: Literal["create_booking", "cancel_booking"]
    revision: int
    body: dict = field(repr=False)
    uid: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    state: WriteState = "planned"
    attempts: int = 0
    result: Booking | None = None
    failure_key: str | None = None
    retry_at: datetime | None = None
    original: Booking | None = None

    def transition(
        self, state: WriteState, *, revision: int | None = None, result: Booking | None = None,
    ):
        if revision is not None:
            if (self.state, state) != ("planned", "planned") or revision < self.revision:
                raise ValueError("Only a planned write can be revalidated for a new revision")
            self.revision = revision
            return
        if state not in WRITE_TRANSITIONS[self.state] or (state == "applied") != (result is not None):
            raise ValueError("Write transition is inconsistent with the saved provider evidence")
        if state == "in_flight":
            self.attempts += 1
        self.state = state
        self.result = result

    @property
    def claim(self) -> tuple:
        if self.kind == "cancel_booking":
            return self.kind, self.uid
        return self.kind, self.body["eventTypeId"], self.body["start"], self.body["attendee"]["email"].casefold()


class HandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    reason: str
    record_key: str
    question_key: str | None
    continuation_key: str | None


@dataclass(frozen=True)
class Route:
    source: Role
    destination: Role
    next_step: str
    revision: int
    payload: HandoffPayload


@dataclass(frozen=True)
class Evidence:
    kind: str
    revision: int
    value: object = field(repr=False)


@dataclass
class Task:
    id: str
    request: str = field(repr=False)
    message: str = field(repr=False)
    received_at: datetime | None = None
    intent: Intent = "unclear"
    role: Role = "triage"
    step: str = "save_intent"
    status: Status = "active"
    revision: int = 0
    inputs: Proposal = field(default_factory=Proposal, repr=False)
    time_zone: str | None = None
    target_booking: Booking | None = None
    replacement: Booking | None = None
    selected_booking: Booking | None = None
    reference_uid: str | None = None
    fresh_revision: int | None = None
    selected_slot: Slot | None = None
    available_slots: tuple[Slot, ...] = ()
    slot_preflight: tuple[int, Slot] | None = None
    replacement_plan: dict | None = field(default=None, repr=False)
    read: Read | None = None
    pending: Question | None = None
    writes: dict[str, Write] = field(default_factory=dict, repr=False)
    operations: dict[str, Write] = field(default_factory=dict, repr=False)
    api_errors: dict[str, APIError] = field(default_factory=dict)
    failure_key: str | None = None
    failed_kind: Literal["read", "write"] | None = None
    failed_write: str | None = None
    reconcile_write: str | None = None
    recovery_result: Literal["reconciliation", "alternatives"] | None = None
    reconciled_booking: Booking | None = None
    verified: dict[str, Evidence] = field(default_factory=dict)
    route: Route | None = None
    outcome: Outcome | None = None
    interrupted_step: tuple[Role, str] | None = None
    run_failure: str | None = None

    @property
    def failed_operation(self) -> Read | Write | None:
        if self.failed_kind == "read":
            return self.read
        if self.failed_kind == "write" and self.failed_write:
            return self.writes.get(self.failed_write)
        return None

    def transition(
        self, role: Role, step: str, *, route: Route | None = None, outcome: Outcome | None = None,
        answer: Question | None = None,
    ):
        """Advance the task lifecycle after validating the current phase and revision."""
        if outcome is not None:
            allowed = (
                self.status == "active" and outcome.status != "active" and outcome.task_id == self.id
                and role == self.role and step == "finished" and route is None
            )
        elif self.status == "awaiting_input":
            allowed = (
                (role, step) == ("confirmation", "save_answer") and self.pending is not None
                and self.pending.revision == self.revision and route is None
            )
        elif self.status in ("recoverable", "incomplete"):
            allowed = (role, step) == ("recovery", "record_recovery") and route is None
        elif self.status != "active":
            allowed = False
        elif self.route is not None:
            allowed = (
                self.step == "handoff" and route is None and self.route.revision == self.revision
                and (role, step) == (self.route.destination, self.route.next_step)
            )
        elif route is not None:
            allowed = (
                self.role == role == route.source and step == "handoff"
                and route.revision == self.revision and route.payload.task_id == self.id
            )
        else:
            allowed = self.role == role and step not in ("handoff", "finished")
        if answer is not None:
            allowed = (
                allowed and self.status == "active" and self.role == role == "confirmation"
                and self.step == step == "save_answer" and self.pending is answer
                and answer.revision == self.revision and route is None and outcome is None
            )
        if not allowed:
            raise ValueError("Task transition does not match the active phase and evidence")
        if answer is not None:
            self.pending = None
            self.revision += 1
        self.role, self.step, self.route = role, step, route
        self.status, self.outcome = (outcome.status if outcome else "active"), outcome

    def save(self, kind: str, value: object = None) -> str:
        key = f"{self.revision}:{kind}:{uuid4().hex}"
        self.verified[key] = Evidence(kind, self.revision, value)
        return key

    def transfer(
        self, destination: Role, step: str, reason: str, record_key: str, *,
        question_key: str | None = None, continuation: bool = False,
    ):
        if self.status != "active" or self.route is not None:
            raise ValueError("Task already has a continuation or an outcome")
        payload = HandoffPayload(
            task_id=self.id, reason=reason, record_key=record_key, question_key=question_key,
            continuation_key=self.save("continuation", (destination, step)) if continuation else None,
        )
        self.transition(self.role, "handoff", route=Route(self.role, destination, step, self.revision, payload))


@dataclass
class WorkflowContext:
    account: Account
    calcom: "CalCom" = field(repr=False)
    clock: Callable[[], datetime]
    delay: Callable[[float], Awaitable[None]]
    wait_budget: float
    tasks: dict[str, Task] = field(default_factory=dict)
    active_task_id: str | None = None
    deadline: datetime | None = None
    write_claims: dict[tuple, str] = field(default_factory=dict, repr=False)
    saved_time_zone: str | None = None
    referenced_uid: str | None = None
    referenced_availability: tuple[int, str, tuple[Slot, ...]] | None = None

    @property
    def task(self) -> Task:
        if self.active_task_id is None:
            raise ValueError("There is no active scheduling task")
        return self.tasks[self.active_task_id]
