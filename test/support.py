"""Acceptance-test contract and a Cal.com HTTP double."""

import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Callable, Protocol


ZONE = "America/New_York"
EVENT_ID = 42
TOKEN = "cal_test_acceptance"


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 11, day, hour, minute, tzinfo=timezone.utc)


def iso(time: datetime) -> str:
    return time.isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class BookingView:
    uid: str
    status: str
    slot: Slot
    title: str = ""
    attendee_name: str = ""
    host_name: str = ""
    event_type_id: int | None = None
    meeting_url: str = ""


@dataclass(frozen=True)
class Handoff:
    source: str
    target: str
    task_id: str


@dataclass(frozen=True)
class Turn:
    task_id: str
    status: str
    message: str
    time_zone: str | None = None
    missing_fields: tuple[str, ...] = ()
    bookings: tuple[BookingView, ...] = ()
    candidates: tuple[BookingView, ...] = ()
    slots: tuple[Slot, ...] = ()
    original: BookingView | None = None
    replacement: BookingView | None = None
    handoffs: tuple[Handoff, ...] = ()


class Driver(Protocol):
    def turn(self, message: str) -> Turn: ...

    def resume(self, task_id: str) -> Turn: ...


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    status: int
    payload: dict
    headers: dict = field(default_factory=dict)


def success(data, status: int = 200, **extra) -> Response:
    return Response(status, {"status": "success", "data": data, **extra})


def error(status: int, message: str) -> Response:
    return Response(status, {"status": "error", "error": {"message": message}})


def booking(uid: str, start: datetime, *, status: str = "accepted", minutes: int = 30,
            name: str = "Alex Rivera", numeric_id: int = 101) -> dict:
    return {
        "id": numeric_id,
        "uid": uid,
        "title": f"Consultation with {name}",
        "status": status,
        "start": iso(start),
        "end": iso(start + timedelta(minutes=minutes)),
        "duration": minutes,
        "eventTypeId": EVENT_ID,
        "eventType": {"id": EVENT_ID, "slug": "consultation"},
        "hosts": [{"id": 1, "name": "Morgan Lee", "timeZone": ZONE}],
        "attendees": [{"name": name, "email": "alex@example.test", "timeZone": ZONE}],
        "location": "https://example.test/meeting",
        "meetingUrl": "https://example.test/meeting",
        "bookingFieldsResponses": {"topic": "Planning"},
        "metadata": {},
    }


class CalComStub:
    def __init__(self):
        self.bookings: dict[str, dict] = {}
        self.slots: list[Slot] = []
        self.requests: list[Request] = []
        self.page_size = 50
        self.created_status = "accepted"
        self.unavailable_reads: set[str] = set()
        self._script: dict[tuple[str, str], deque] = {}

    @property
    def writes(self) -> list[Request]:
        return [request for request in self.requests if request.method == "POST"]

    def script(self, method: str, path: str, *actions: Response | Callable[[Request], Response]):
        self._script.setdefault((method, path), deque()).extend(actions)

    def request(self, method: str, path: str, *, headers: dict, query: dict | None = None,
                body: dict | None = None) -> Response:
        """The adapter calls this at the actual application's HTTP boundary."""
        # Preserve the wire payload even when the caller later mutates its dictionary.
        request = Request(method, path, dict(query or {}), json.loads(json.dumps(body or {})))
        self.requests.append(request)
        self._check_headers(request, {name.lower(): value for name, value in headers.items()})
        actions = self._script.get((method, path))
        if actions:
            action = actions.popleft()
            return action(request) if callable(action) else action
        if method == "GET" and path in self.unavailable_reads:
            return error(503, "Service unavailable")
        return self.respond(request)

    def _check_headers(self, request: Request, headers: dict):
        route = request.method, request.path
        if route == ("GET", "/v2/slots"):
            version = "2024-09-04"
        elif route == ("GET", "/v2/bookings"):
            version = "2026-05-01"
        else:
            version = "2026-02-25"
        assert headers.get("cal-api-version") == version, f"Wrong API version for {route}"
        assert headers.get("authorization") == f"Bearer {TOKEN}", f"Missing test credential for {route}"
        if request.method == "POST":
            assert headers.get("content-type", "").split(";")[0] == "application/json", route

    def respond(self, request: Request) -> Response:
        """Apply the normal provider behavior, including write side effects."""
        route = request.method, request.path
        if route == ("GET", "/v2/slots"):
            assert str(request.query.get("eventTypeId")) == str(EVENT_ID)
            assert request.query.get("format") == "range"
            start = parse_time(request.query["start"])
            end = parse_time(request.query["end"])
            response: dict[str, list[dict]] = {}
            for slot in self.slots:
                if start <= slot.start < end:
                    response.setdefault(slot.start.date().isoformat(), []).append(
                        {"start": iso(slot.start), "end": iso(slot.end)}
                    )
            return success(response)
        if route == ("GET", "/v2/bookings"):
            return self._list(request.query)
        if route == ("POST", "/v2/bookings"):
            return self._create(request.body)
        if request.path.startswith("/v2/bookings/"):
            uid, _, action = request.path.removeprefix("/v2/bookings/").partition("/")
            if route[0] == "GET" and not action:
                return success(self.bookings[uid]) if uid in self.bookings else error(404, "Booking not found")
            if route[0] == "POST" and action == "cancel":
                if uid not in self.bookings:
                    return error(404, "Booking not found")
                reason = request.body.get("cancellationReason")
                if not isinstance(reason, str) or not reason.strip():
                    return error(400, "Cancellation reason is required")
                self.bookings[uid] = {**self.bookings[uid], "status": "cancelled"}
                return success(self.bookings[uid])
        raise AssertionError(f"Unexpected Cal.com request {route}")

    def _list(self, query: dict) -> Response:
        records = list(self.bookings.values())
        if "eventTypeId" in query:
            records = [item for item in records if str(item["eventTypeId"]) == str(query["eventTypeId"])]
        if "bookingUid" in query:
            records = [item for item in records if item["uid"] == query["bookingUid"]]
        for parameter, field_name in (("attendeeEmail", "email"), ("attendeeName", "name")):
            if parameter in query:
                value = query[parameter].casefold()
                records = [item for item in records if any(
                    value in attendee[field_name].casefold() for attendee in item["attendees"]
                )]
        if "afterStart" in query:
            records = [item for item in records if parse_time(item["start"]) > parse_time(query["afterStart"])]
        if "beforeEnd" in query:
            records = [item for item in records if parse_time(item["end"]) < parse_time(query["beforeEnd"])]
        size = min(int(query.get("limit", self.page_size)), self.page_size)
        offset = int(query.get("cursor", "page:0").removeprefix("page:"))
        next_offset = offset + size
        has_more = next_offset < len(records)
        return success(records[offset:next_offset], pagination={
            "nextCursor": f"page:{next_offset}" if has_more else None,
            "hasMore": has_more,
        })

    def _create(self, body: dict) -> Response:
        start = parse_time(body["start"])
        assert start.utcoffset() == timedelta(0), "Cal.com create requires a UTC start"
        assert body["eventTypeId"] == EVENT_ID
        assert body["attendee"]["name"] == "Alex Rivera"
        assert body["attendee"]["email"] == "alex@example.test"
        assert body["attendee"]["timeZone"] == ZONE
        assert body["bookingFieldsResponses"]["topic"] == "Planning"
        assert body.get("metadata"), "Expected the reconciliation marker in metadata"
        if not any(slot.start == start for slot in self.slots):
            return error(409, "The requested time slot is no longer available")
        numeric_id = 1000 + len(self.bookings)
        uid = f"created-{numeric_id}"
        data = booking(uid, start, status=self.created_status, numeric_id=numeric_id)
        data["metadata"] = body["metadata"]
        self.bookings[uid] = data
        return success(data, status=201)


@dataclass(frozen=True)
class Environment:
    calcom: CalComStub
    now: datetime = utc(2, 12)
    conversation_id: str = "acceptance-conversation"
    api_key: str = TOKEN
    event_type_id: int = EVENT_ID
    event_type_name: str = "Consultation"
    event_duration_minutes: int = 30
    required_booking_field: str = "topic"


def load_factory() -> Callable[[Environment], Driver] | None:
    target = os.environ.get("CALCOM_TEST_ADAPTER")
    if not target:
        return None
    module, separator, name = target.partition(":")
    if not (module and separator and name):
        raise ValueError("CALCOM_TEST_ADAPTER must use module:factory syntax")
    factory = getattr(import_module(module), name)
    if not callable(factory):
        raise TypeError(f"{target} is not a factory")
    return factory
