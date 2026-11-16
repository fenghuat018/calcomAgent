"""Scheduling acceptance adapters with a controlled or live interpreter."""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from uuid import uuid4

from agents import Model, ModelResponse, Usage
from dotenv import load_dotenv
from openai.types.responses import ResponseFunctionToolCall

from tools.calcom_client import Response
from workflow import SchedulingWorkflow
from workflow_context import Account, EventType

from test.support import BookingView, Handoff, Slot, Turn
from zoneinfo import ZoneInfo


class AcceptanceInterpreter(Model):
    """Supply structured interpretations of the acceptance suite's sample utterances."""

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema,
        handoffs, tracing, **kwargs,
    ):
        message = input if isinstance(input, str) else next(
            item["content"] for item in reversed(input) if item.get("role") == "user"
        )
        context = json.loads(system_instructions.splitlines()[-1])
        names = {tool.name for tool in tools}
        if "save_intent" in names:
            name = "save_intent"
            args = {"proposal": self._interpret_request(message)}
        elif "save_answer" in names:
            name = "save_answer"
            args = {"answer": (
                {"fields": self._interpret_request(message)}
                if context["expected_fields"] == ["intent"] else self._interpret_answer(message, context)
            )}
        else:
            raise AssertionError(f"Unexpected interpretation tools: {names}")
        call = ResponseFunctionToolCall(
            type="function_call", name=name, arguments=json.dumps(args), call_id=uuid4().hex,
        )
        return ModelResponse([call], Usage(), None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError

    @staticmethod
    def _interpret_request(message: str) -> dict:
        lowered = message.casefold()
        if lowered.startswith("book"):
            intent = "create_booking"
        elif lowered.startswith("move"):
            intent = "reschedule_booking"
        elif lowered.startswith("cancel"):
            intent = "cancel_booking"
        elif lowered.startswith("list"):
            intent = "list_bookings"
        elif "available" in lowered:
            intent = "get_availability"
        elif lowered.startswith(("show details", "when and where")):
            intent = "get_booking"
        else:
            intent = "unclear"

        date = re.search(r"\bNov(?:ember)? (\d+)(?:, (\d{4}))?\b", message)
        times = re.findall(r"\b(?:\d{1,2}(?::\d{2})? [AP]M|noon)\b", message, re.I)
        zone = re.search(r"\bAmerica/[A-Za-z_]+", message)
        email = re.search(r"\b[^\s,]+@example\.test\b", message)
        topic = re.search(r"topic is ([^.]+)", message)
        uid = re.search(r"\bbooking ([\w]+-[\w-]+)\b", message)
        proposal = {"intent": intent}

        def local(label: str) -> str:
            parsed = datetime.strptime("12:00 PM" if label == "noon" else (
                label if ":" in label else label.replace(" ", ":00 ")
            ), "%I:%M %p")
            return f"{day}T{parsed.hour:02}:{parsed.minute:02}:00"

        if zone:
            proposal["time_zone"] = zone.group()
        if "consultation" in lowered and intent in ("create_booking", "get_availability"):
            proposal["event_type"] = "Consultation"
        if uid:
            proposal["uid"] = uid.group(1)
        if date:
            number, year = date.groups()
            day = f"{year or '2030'}-11-{int(number):02}"
            tomorrow = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
            if intent in ("list_bookings", "get_availability"):
                proposal["range_start"] = local(times[0]) if times else day
                proposal["range_end"] = local(times[1]) if len(times) >= 2 else tomorrow
            elif intent in ("create_booking", "reschedule_booking"):
                proposal["start"] = local(times[-1]) if times else day
            elif not uid:
                proposal["target_start"] = local(times[0]) if times else day
                if not times:
                    proposal["target_end"] = tomorrow
            if intent == "reschedule_booking" and len(times) > 1:
                proposal["target_start"] = local(times[0])
        if intent == "create_booking" and "Alex Rivera" in message:
            proposal["attendee_name"] = "Alex Rivera"
        if intent in ("cancel_booking", "reschedule_booking", "get_booking") and "Alex" in message:
            proposal["attendee_clue"] = "Alex Rivera" if "Alex Rivera" in message else "Alex"
        if email:
            proposal["attendee_email"] = email.group()
        if topic:
            proposal["booking_fields"] = [{"field": "topic", "value": topic.group(1)}]
        return proposal

    @staticmethod
    def _interpret_answer(message: str, context: dict) -> dict:
        fields = context["expected_fields"]
        if fields == ["attendee.email"]:
            return {"fields": {"attendee_email": message}}
        if fields == ["attendee.timeZone"]:
            return {"fields": {"time_zone": message}}
        if fields == ["bookingFieldsResponses.topic"]:
            return {"fields": {"booking_fields": [{"field": "topic", "value": message}]}}
        if "second 1:30" in message:
            return {"choice": 2}
        for number, label in enumerate(context["slots"] or context["bookings"], 1):
            if "11 AM" in message and "11:00 AM" in label:
                return {"choice": number}
            if "noon" in message and "12:00 PM" in label:
                return {"choice": number}
        return {}


class Driver:
    def __init__(self, environment, model: str | Model | None = None):
        self.conversation_id = environment.conversation_id
        self._loop = None

        def transport(method, path, *, headers, query, body):
            response = environment.calcom.request(method, path, headers=headers, query=query, body=body)
            return Response(response.status, response.payload, response.headers)

        async def delay(_):
            pass

        event = EventType(
            environment.event_type_id, environment.event_type_name, environment.event_duration_minutes,
            (environment.required_booking_field,),
        )
        self.workflow = SchedulingWorkflow(
            Account((event,)), environment.api_key, model=model or AcceptanceInterpreter(),
            transport=transport, clock=lambda: environment.now, delay=delay,
        )

    @staticmethod
    def convert(outcome):
        def view(booking):
            if booking is None:
                return None
            zone = ZoneInfo(outcome.time_zone or booking.timeZone or "UTC")
            slot = booking.slot.local(zone)
            return BookingView(
                booking.calcom_uid, booking.status, Slot(slot.start, slot.end), booking.title,
                booking.name, booking.host_name, booking.event_type_id, booking.meeting_url,
            )

        return Turn(
            outcome.task_id, outcome.status, outcome.message, outcome.time_zone, outcome.missing_fields,
            tuple(view(row) for row in outcome.bookings), tuple(view(row) for row in outcome.candidates),
            tuple(Slot(slot.start, slot.end) for slot in outcome.slots),
            view(outcome.original), view(outcome.replacement),
            tuple(Handoff(event.source, event.target, event.task_id) for event in outcome.handoffs),
        )

    def turn(self, message: str) -> Turn:
        return self.convert(self._run(self.workflow.turn(message, self.conversation_id)))

    def resume(self, task_id: str) -> Turn:
        return self.convert(self._run(self.workflow.resume(task_id, self.conversation_id)))

    def _run(self, coroutine):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coroutine)

    def close(self):
        if self._loop is not None:
            self._loop.run_until_complete(self.workflow.close())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()
            self._loop = None


def make_driver(environment):
    return Driver(environment)


def make_online_driver(environment):
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("The online acceptance adapter requires OPENAI_API_KEY")
    return Driver(environment, model=os.environ.get("OPENAI_MODEL") or "gpt-5-mini")
