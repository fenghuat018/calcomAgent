"""Verify the SDK agent definitions, phase gates, and cross-turn checkpoints."""

from dataclasses import replace
from datetime import timedelta
from unittest import IsolatedAsyncioTestCase

from agents import RunContextWrapper

from scheduling_agents import create_agents
from test.adapter import AcceptanceInterpreter, Driver
from test.support import CalComStub, Environment, Slot, booking, error, utc
from test.test_acceptance import BOOKINGS, CREATE, RANGE, SLOTS
from workflow_context import Task


class AgentDefinitions(IsolatedAsyncioTestCase):
    def test_sdk_definitions_match_handoff_and_tool_allowlists(self):
        agents = create_agents()
        expected = {
            "triage": {"booking", "retrieval", "confirmation"},
            "booking": {"retrieval", "confirmation", "recovery"},
            "retrieval": {"booking", "recovery", "confirmation"},
            "confirmation": {"triage", "booking", "retrieval"},
            "recovery": {"retrieval", "booking", "confirmation"},
        }
        provider_tools = {
            "booking": {"create_booking", "cancel_booking"},
            "retrieval": {"list_bookings", "get_booking", "get_availability"},
        }
        calcom_tools = provider_tools["booking"] | provider_tools["retrieval"]
        self.assertEqual(set(agents), set(expected))
        for role, agent in agents.items():
            with self.subTest(role=role):
                self.assertEqual({edge.agent_name for edge in agent.handoffs}, expected[role])
                self.assertEqual(
                    {tool.name for tool in agent.tools} & calcom_tools, provider_tools.get(role, set()),
                )

    async def test_sdk_handoff_checks_task_and_saved_evidence(self):
        driver = Driver(Environment(CalComStub()))
        context = driver.workflow._conversation(driver.conversation_id).context
        task = Task("active-task", "Cancel booking original-uid.", "Cancel booking original-uid.")
        context.tasks[task.id] = task
        context.active_task_id = task.id
        wrapper = RunContextWrapper(context)
        triage = driver.workflow.agents["triage"]
        routes = {edge.agent_name: edge for edge in triage.handoffs}
        self.assertFalse(any([await edge.is_enabled(wrapper, triage) for edge in routes.values()]))
        key = task.save("intent", "cancel_booking")
        task.transfer("booking", "prepare_booking", "A saved request.", key)
        booking = routes["booking"]
        self.assertTrue(await booking.is_enabled(wrapper, triage))
        self.assertFalse(await routes["retrieval"].is_enabled(wrapper, triage))

        with self.assertRaisesRegex(ValueError, "Handoff does not match"):
            await booking.on_invoke_handoff(
                wrapper, task.route.payload.model_copy(update={"task_id": "different-task"}).model_dump_json(),
            )
        with self.assertRaisesRegex(ValueError, "Handoff does not match"):
            await booking.on_invoke_handoff(
                wrapper, task.route.payload.model_copy(update={"record_key": "missing"}).model_dump_json(),
            )
        await booking.on_invoke_handoff(wrapper, task.route.payload.model_dump_json())
        self.assertEqual((task.role, task.step), ("booking", "prepare_booking"))
        self.assertFalse(await booking.is_enabled(wrapper, triage))
        booking_tools = await driver.workflow.agents["booking"].get_all_tools(wrapper)
        self.assertEqual({tool.name for tool in booking_tools}, {"read_booking_state", "prepare_booking"})


class WorkflowCheckpoints(IsolatedAsyncioTestCase):
    def setUp(self):
        self.calcom = CalComStub()
        self.environment = Environment(self.calcom)
        self.driver = Driver(self.environment)
        self.workflow = self.driver.workflow
        self.conversation = self.environment.conversation_id
        start = utc(5, 15)
        self.calcom.slots = [Slot(start, start + timedelta(minutes=30))]

    async def test_unrelated_request_and_clarification_keep_separate_task_ids(self):
        pending = await self.workflow.turn(CREATE.replace(", alex@example.test", ""), self.conversation)
        listing = await self.workflow.turn(f"List bookings for {RANGE}.", self.conversation)
        answer = await self.workflow.turn("alex@example.test", self.conversation)
        self.assertEqual((pending.status, listing.status, answer.status),
                         ("awaiting_input", "completed", "completed"))
        self.assertNotEqual(pending.task_id, listing.task_id)
        self.assertEqual(answer.task_id, pending.task_id)
        self.assertTrue(answer.handoffs)
        self.assertTrue(all(item.task_id == pending.task_id for item in answer.handoffs))

    async def test_booking_detail_requests_start_fresh_while_a_time_window_is_pending(self):
        requests = (
            "what is the time of the meeting with alice",
            "check the detail of my meeting with alice",
        )

        class BookingDetailsInterpreter(AcceptanceInterpreter):
            @staticmethod
            def _interpret_request(message):
                if message in requests:
                    return {"attendee_clue": "Alice"}
                return AcceptanceInterpreter._interpret_request(message)

        workflow = Driver(self.environment, model=BookingDetailsInterpreter()).workflow
        self.calcom.slots = []
        self.calcom.bookings["alice-uid"] = booking("alice-uid", utc(5, 16), name="Alice")
        pending = await workflow.turn(CREATE, self.conversation)
        self.assertEqual((pending.status, pending.missing_fields), ("awaiting_input", ("range",)))

        for message in requests:
            with self.subTest(message=message):
                await workflow.resume(pending.task_id, self.conversation)
                result = await workflow.turn(message, self.conversation)
                self.assertEqual(result.status, "completed", result.message)
                self.assertNotEqual(result.task_id, pending.task_id)
                self.assertEqual([item.calcom_uid for item in result.bookings], ["alice-uid"])
                self.assertIn("11:00 AM", result.message)
                self.assertEqual(
                    [(handoff.source, handoff.target) for handoff in result.handoffs],
                    [("triage", "retrieval")],
                )

        original = await workflow.resume(pending.task_id, self.conversation)
        self.assertEqual(original.question_key, pending.question_key)
        self.assertEqual(original.message, pending.message)
        self.assertEqual(self.calcom.writes, [])

    async def test_bare_option_answers_the_latest_question_with_an_older_task_pending(self):
        older = await self.workflow.turn(CREATE.replace(", alex@example.test", ""), self.conversation)
        self.calcom.bookings["original-uid"] = booking("original-uid", utc(5, 15))
        self.calcom.bookings["other-uid"] = booking("other-uid", utc(5, 16), numeric_id=102)
        latest = await self.workflow.turn(
            "Cancel my call with Alex on November 5, America/New_York.", self.conversation,
        )
        self.assertEqual(older.status, "awaiting_input")
        self.assertEqual(latest.status, "awaiting_input")
        self.assertNotEqual(older.task_id, latest.task_id)
        self.assertEqual([item.calcom_uid for item in latest.candidates], ["original-uid", "other-uid"])

        result = await self.workflow.turn("1", self.conversation)
        self.assertEqual((result.status, result.task_id), ("completed", latest.task_id))
        self.assertEqual([request.path for request in self.calcom.writes], [f"{BOOKINGS}/original-uid/cancel"])
        self.assertEqual(self.calcom.bookings["other-uid"]["status"], "accepted")
        still_pending = await self.workflow.resume(older.task_id, self.conversation)
        self.assertEqual(still_pending.question_key, older.question_key)

    async def test_bare_task_reference_reopens_its_question_for_the_next_reply(self):
        older = await self.workflow.turn(CREATE.replace(", alex@example.test", ""), self.conversation)
        newer = await self.workflow.turn(CREATE.replace(" The topic is Planning.", ""), self.conversation)
        listing = await self.workflow.turn(f"List bookings for {RANGE}.", self.conversation)
        self.assertEqual((older.status, newer.status, listing.status),
                         ("awaiting_input", "awaiting_input", "completed"))

        ambiguous = await self.workflow.turn("alex@example.test", self.conversation)
        self.assertIn("Which task are you answering?", ambiguous.message)
        reopened = await self.workflow.turn(older.task_id, self.conversation)
        self.assertEqual((reopened.task_id, reopened.question_key), (older.task_id, older.question_key))
        self.assertEqual(reopened.message, older.message)
        self.assertEqual(reopened.handoffs, ())
        self.assertEqual(self.calcom.writes, [])

        result = await self.workflow.turn("alex@example.test", self.conversation)
        self.assertEqual((result.status, result.task_id), ("completed", older.task_id))
        self.assertEqual(len(self.calcom.writes), 1)
        still_pending = await self.workflow.resume(newer.task_id, self.conversation)
        self.assertEqual(still_pending.question_key, newer.question_key)

    async def test_past_time_clarification_explains_rejection_and_accepts_a_future_reply(self):
        class TimeReplies(AcceptanceInterpreter):
            @staticmethod
            def _interpret_answer(message, context):
                starts = {
                    "12:15 AM today": "D+0T00:15:00",
                    "tomorrow at 10 AM": "D+1T10:00:00",
                }
                return {"fields": {"start": starts[message]}} if message in starts else {}

        environment = replace(self.environment, now=utc(5, 14))
        workflow = Driver(environment, model=TimeReplies()).workflow
        self.calcom.slots = [Slot(utc(6, 15), utc(6, 15, 30))]
        first = await workflow.turn(CREATE.replace("10 AM", "12:00 AM"), self.conversation)
        self.assertEqual(first.status, "awaiting_input")
        self.assertEqual(first.missing_fields, ("start",))
        self.assertIn("12:00 AM (EST) has already passed", first.message)
        self.assertIn("09:00 AM (EST) in America/New_York", first.message)
        self.assertIn("tomorrow at 10 AM", first.message)

        repeated = await workflow.turn("12:15 AM today", self.conversation)
        self.assertEqual(repeated.task_id, first.task_id)
        self.assertIn("12:15 AM (EST) has already passed", repeated.message)
        unclear = await workflow.turn("whenever", self.conversation)
        self.assertIn("I couldn’t use that reply", unclear.message)
        self.assertIn("later date and time", unclear.message)
        self.assertEqual(self.calcom.requests, [])

        booked = await workflow.turn("tomorrow at 10 AM", self.conversation)
        self.assertEqual(booked.status, "completed")
        self.assertEqual(booked.task_id, first.task_id)
        self.assertEqual(booked.bookings[0].slot.start, utc(6, 15))
        self.assertEqual(len(self.calcom.writes), 1)

    async def test_replayed_turn_id_returns_saved_outcome(self):
        first = await self.workflow.turn(CREATE, self.conversation, turn_id="turn-1")
        await self.workflow.turn(f"List bookings for {RANGE}.", self.conversation)
        repeated = await self.workflow.turn(CREATE, self.conversation, turn_id="turn-1")
        self.assertEqual(first.status, "completed")
        self.assertEqual(first.task_id, repeated.task_id)
        self.assertEqual(repeated.handoffs, ())
        self.assertEqual(len(self.calcom.writes), 1)

    async def test_retry_after_longer_than_turn_budget_defers_the_read(self):
        delay_until = error(429, "Rate limited")
        delay_until.headers["Retry-After"] = "60"
        self.calcom.script("GET", SLOTS, delay_until)
        first = await self.workflow.turn(f"Show available Consultation slots for {RANGE}?", self.conversation)
        resumed = await self.workflow.resume(first.task_id, self.conversation)
        self.assertEqual(resumed.status, "recoverable")
        self.assertIn("Earliest retry", resumed.message)
        self.assertEqual(len(self.calcom.requests), 1)

    async def test_uncertain_create_blocks_an_equivalent_write_in_another_conversation(self):
        def timeout_after_dispatch(request):
            raise TimeoutError("Response lost")

        self.calcom.script("POST", BOOKINGS, timeout_after_dispatch)
        first = await self.workflow.turn(CREATE, self.conversation)
        self.assertEqual(first.status, "recoverable")
        second = await self.workflow.turn(CREATE, "another-conversation")
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertEqual(second.status, "failed")
        self.assertEqual(len(self.calcom.writes), 1)

    async def test_restart_lookup_uses_current_provider_data_without_a_write(self):
        self.calcom.bookings["original-uid"] = booking("original-uid", utc(5, 15))
        result = await self.workflow.resume("old-task", self.conversation, booking_reference="101")
        self.assertEqual(result.status, "completed")
        self.assertNotEqual(result.task_id, "old-task")
        self.assertEqual([row.calcom_uid for row in result.bookings], ["original-uid"])
        self.assertEqual([item.path for item in self.calcom.requests], [BOOKINGS, f"{BOOKINGS}/original-uid"])
        self.assertEqual(self.calcom.writes, [])

    async def test_repeated_answer_to_old_question_is_rejected(self):
        pending = await self.workflow.turn(CREATE.replace(", alex@example.test", ""), self.conversation)
        booked = await self.workflow.turn(
            "alex@example.test", self.conversation, reply_to=pending.task_id, question_key=pending.question_key,
        )
        repeated = await self.workflow.turn(
            "alex@example.test", self.conversation, reply_to=pending.task_id, question_key=pending.question_key,
        )
        self.assertEqual(booked.status, "completed")
        self.assertEqual(repeated.status, "not_found")
        self.assertEqual(len(self.calcom.writes), 1)

    async def test_long_provider_retry_after_is_saved_on_the_write(self):
        response = error(429, "Rate limited")
        response.headers["Retry-After"] = "60"
        self.calcom.script("POST", BOOKINGS, response)
        outcome = await self.workflow.turn(CREATE, self.conversation)
        self.assertEqual(outcome.status, "recoverable")
        task = self.workflow._conversation(self.conversation).context.tasks[outcome.task_id]
        self.assertEqual(task.writes["create_booking"].retry_at, self.environment.now + timedelta(seconds=60))
        result = await self.workflow.resume(outcome.task_id, self.conversation)
        self.assertIn("Earliest retry", result.message)
        self.assertEqual(len(self.calcom.writes), 1)

    async def test_explicit_range_and_zone_survive_incomplete_intent_clarification(self):
        class IncompleteRange(AcceptanceInterpreter):
            @staticmethod
            def _interpret_request(message):
                return {"intent": "list_bookings"} if message.startswith("List ") else {"intent": "unclear"}

        workflow = Driver(self.environment, model=IncompleteRange()).workflow
        pending = await workflow.turn("I need help with my schedule.", self.conversation)
        result = await workflow.turn(f"List bookings for {RANGE}.", self.conversation)
        self.assertEqual((pending.status, result.status), ("awaiting_input", "completed"))
        self.assertEqual(result.task_id, pending.task_id)
        self.assertEqual(result.time_zone, "America/New_York")
        self.assertEqual(len(self.calcom.requests), 1)

    async def test_explicit_dst_choice_takes_precedence_over_model_index(self):
        class WrongIndex(AcceptanceInterpreter):
            @staticmethod
            def _interpret_answer(message, context):
                return {"choice": 1}

        starts = (utc(3, 5, 30), utc(3, 6, 30))
        self.calcom.slots = [Slot(start, start + timedelta(minutes=30)) for start in starts]
        workflow = Driver(self.environment, model=WrongIndex()).workflow
        message = CREATE.replace("November 5, 2030 at 10 AM", "November 3, 2030 at 1:30 AM")
        pending = await workflow.turn(message, self.conversation)
        result = await workflow.turn("The second 1:30 AM, after the clocks go back (UTC-05:00).", self.conversation)
        self.assertEqual((pending.status, result.status), ("awaiting_input", "completed"))
        self.assertEqual(result.task_id, pending.task_id)
        self.assertEqual(result.bookings[0].slot.start, starts[1])
        self.assertEqual(len(self.calcom.writes), 1)
