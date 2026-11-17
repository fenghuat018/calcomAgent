"""Cal.com API v2 transport and validated endpoint contracts."""

import asyncio
import http.client
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote, urlencode

from workflow_context import APIError, Booking, Slot, instant, iso


BASE_URL = "https://api.cal.com"
BOOKINGS = "/v2/bookings"
BOOKING_VERSION = "2026-02-25"
OPERATION_MARKER = "schedulingOperationId"
UID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


@dataclass(frozen=True)
class Response:
    status: int
    payload: object
    headers: dict[str, str]


class PreDispatchError(OSError):
    """The transport established that it never sent the HTTP request."""


class ProviderFailure(Exception):
    def __init__(self, error: APIError):
        super().__init__(error.error_message)
        self.error = error


def https_request(method, path, *, headers, query, body) -> Response:
    connection = http.client.HTTPSConnection("api.cal.com", timeout=15)
    path = f"{path}?{urlencode(query)}" if query else path
    payload = json.dumps(body) if method == "POST" else None
    try:
        try:
            connection.connect()
        except OSError as error:
            raise PreDispatchError from error
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        try:
            data = json.loads(response.read())
        except (ValueError, UnicodeError, OSError, http.client.HTTPException):
            data = None
        return Response(response.status, data, dict(response.getheaders()))
    finally:
        connection.close()


def retry_delay(headers: dict[str, str], now: datetime) -> float:
    value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if value is None:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def text(value) -> str:
    return value if isinstance(value, str) else ""


def normalize_booking(row: dict) -> Booking:
    uid = row["uid"]
    numeric_id, event_id = row["id"], row.get("eventTypeId")
    if event_id is None:
        event_id = row["eventType"]["id"]
    status = row["status"]
    if (
        not isinstance(uid, str) or not UID.fullmatch(uid)
        or type(numeric_id) is not int or type(event_id) is not int
        or numeric_id <= 0 or event_id <= 0
        or not isinstance(status, str) or not status.strip()
    ):
        raise ValueError("Invalid booking identity or status")
    slot = Slot(instant(row["start"]), instant(row["end"]))
    attendee = next(iter(row.get("attendees") or ()), {})
    host = next(iter(row.get("hosts") or ()), {})
    fields = row.get("bookingFieldsResponses") or {}
    metadata = row.get("metadata") or {}
    location = row.get("location")
    if not all(isinstance(value, dict) for value in (attendee, host, fields, metadata)):
        raise ValueError("Invalid booking details")
    return Booking(
        booking_uid=numeric_id, calcom_uid=uid, name=text(attendee.get("name")),
        timeZone=text(attendee.get("timeZone")), slots={"start": iso(slot.start), "end": iso(slot.end)},
        event_type_id=event_id, status=status.casefold(), title=text(row.get("title")),
        attendee_email=text(attendee.get("email")), host_name=text(host.get("name")),
        location=text(location), meeting_url=text(row.get("meetingUrl")),
        booking_fields=fields, operation_marker=text(metadata.get(OPERATION_MARKER)) or None,
        rescheduled_to=text(row.get("rescheduledToUid")) or None,
        location_input=location if isinstance(location, dict) else None,
    )


def normalize_page(data, envelope, prior_cursors: set[str]) -> tuple[list[Booking], str | None]:
    pagination = envelope["pagination"]
    has_more, cursor = pagination["hasMore"], pagination["nextCursor"]
    if not isinstance(data, list) or type(has_more) is not bool:
        raise ValueError("Invalid booking page")
    if has_more and (not isinstance(cursor, str) or not cursor or cursor in prior_cursors):
        raise ValueError("Invalid booking pagination cursor")
    if not has_more and cursor is not None:
        raise ValueError("Unexpected cursor on final booking page")
    return [normalize_booking(row) for row in data], cursor


def normalize_slots(data, bounds: Slot) -> tuple[Slot, ...]:
    if not isinstance(data, dict) or any(not isinstance(rows, list) for rows in data.values()):
        raise ValueError("Invalid availability data")
    slots = {
        Slot(instant(row["start"]), instant(row["end"]))
        for rows in data.values() for row in rows
    }
    return tuple(sorted(slot for slot in slots if bounds.start <= slot.start and slot.end <= bounds.end))


def error_details(method, endpoint, status, payload, retry_after=0, *, redact=()) -> APIError:
    details = payload.get("error", {}) if isinstance(payload, dict) else {}
    if isinstance(details, dict):
        nested = details.get("details")
        nested_message = nested.get("message") if isinstance(nested, dict) else None
        if isinstance(nested_message, list):
            nested_message = " ".join(item for item in nested_message if isinstance(item, str))
        message = text(nested_message) or text(details.get("message"))
        code = text(details.get("code"))
    else:
        message, code = text(details), ""
    provider_text = f"{code} {message}".casefold()
    field = None
    provider_detail = None
    if status in (401, 403):
        category, summary = "authorization", "Cal.com refused access. Check the account and API key."
    elif status == 404:
        category, summary = "missing", "Cal.com could not find the requested resource."
    elif status == 429:
        category, summary = "temporary", "Cal.com requested a retry after rate limiting."
    elif status in (400, 409, 422) and "slot" in provider_text and any(
        token in provider_text for token in ("available", "booked", "taken", "conflict")
    ):
        category, summary = "slot_conflict", "Cal.com rejected the selected slot as unavailable."
    elif status in (400, 422):
        category, summary = "invalid_input", "Cal.com rejected the request inputs."
        match = re.search(
            r"\b(bookingFieldsResponses\.[\w-]+|attendee\.(?:name|email|timeZone)|"
            r"start|lengthInMinutes|location|cancellationReason)\b", f"{code} {message}",
        )
        field = match.group(1) if match else None
        if message:
            for value in redact:
                message = message.replace(value, "[redacted]")
            message = re.sub(r"https?://\S+|[^\s<>]+@[^\s<>]+", "[redacted]", message)
            message = "".join(char if char.isprintable() else " " for char in message)
            provider_detail = " ".join(message.split())[:350] or None
    elif status >= 500 or status == 408 or 200 <= status < 300:
        category = "uncertain" if method == "POST" else "temporary"
        summary = "Cal.com returned an unavailable or invalid response."
    else:
        category, summary = "rejected", "Cal.com rejected the request."
    return APIError(method, BASE_URL + endpoint, {}, status, summary, category, retry_after, field, provider_detail)


class CalCom:
    def __init__(self, api_key: str, clock: Callable[[], datetime], transport=https_request):
        if not api_key:
            raise ValueError("CALCOM_API_KEY is required")
        self._api_key = api_key
        self._clock = clock
        self._transport = transport

    async def _request(self, method, path, endpoint, version, normalize, *, query=None, body=None, verify=None):
        headers = {"Authorization": f"Bearer {self._api_key}", "cal-api-version": version}
        redacted_body = {key: "[redacted]" for key in body} if body else {}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            response = await asyncio.to_thread(
                self._transport, method, path, headers=headers, query=query or {}, body=body,
            )
        except (OSError, http.client.HTTPException) as error:
            category = "temporary" if method == "GET" or isinstance(error, PreDispatchError) else "uncertain"
            raise ProviderFailure(APIError(
                method, BASE_URL + endpoint, redacted_body, 0, "The Cal.com connection failed.", category,
            )) from None

        envelope = response.payload
        if (
            200 <= response.status < 300 and isinstance(envelope, dict)
            and envelope.get("status") == "success"
        ):
            try:
                result = normalize(envelope["data"], envelope)
                if verify is None or verify(result):
                    return result
            except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError):
                pass
        sensitive = (self._api_key,)
        if "{bookingUid}" in endpoint:
            sensitive += (path.split("/")[3],)
        error = error_details(
            method, endpoint, response.status, envelope, retry_delay(response.headers, self._clock()),
            redact=sensitive,
        )
        raise ProviderFailure(replace(error, request_body=redacted_body))

    async def list_bookings(self, query: dict, cursor: str | None, prior_cursors: set[str]):
        params = {**query, **({"cursor": cursor} if cursor else {})}
        return await self._request(
            "GET", BOOKINGS, BOOKINGS, "2026-05-01",
            lambda data, envelope: normalize_page(data, envelope, prior_cursors), query=params,
        )

    async def get_booking(self, uid: str) -> Booking:
        return await self._request(
            "GET", f"{BOOKINGS}/{quote(uid, safe='')}", BOOKINGS + "/{bookingUid}", BOOKING_VERSION,
            lambda data, _: normalize_booking(data),
        )

    async def get_availability(self, query: dict, bounds: Slot) -> tuple[Slot, ...]:
        return await self._request(
            "GET", "/v2/slots", "/v2/slots", "2024-09-04",
            lambda data, _: normalize_slots(data, bounds),
            query={**query, "start": iso(bounds.start), "end": iso(bounds.end), "format": "range"},
        )

    async def create_booking(self, body: dict, *, verify=None) -> Booking:
        return await self._request(
            "POST", BOOKINGS, BOOKINGS, BOOKING_VERSION,
            lambda data, _: normalize_booking(data), body=body, verify=verify,
        )

    async def cancel_booking(self, uid: str, body: dict, *, verify=None) -> Booking:
        return await self._request(
            "POST", f"{BOOKINGS}/{quote(uid, safe='')}/cancel", BOOKINGS + "/{bookingUid}/cancel",
            BOOKING_VERSION, lambda data, _: normalize_booking(data), body=body, verify=verify,
        )
