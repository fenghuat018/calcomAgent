"""Run the scheduling specification through the application's public workflow."""

from datetime import timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

from test.support import (
    ZONE,
    CalComStub,
    Environment,
    Slot,
    booking,
    error,
    load_factory,
    parse_time,
    utc,
)


BOOKINGS = "/v2/bookings"
SLOTS = "/v2/slots"
ORIGINAL = f"{BOOKINGS}/original-uid"
CANCEL = f"{ORIGINAL}/cancel"
RANGE = "November 5, 2030 from 10 AM to noon America/New_York"
CREATE = (
    "Book a Consultation on November 5, 2030 at 10 AM America/New_York "
    "for Alex Rivera, alex@example.test. The topic is Planning."
)
MOVE = "Move my Consultation with Alex Rivera from Nov 5 at 10 AM to 11 AM that day, America/New_York."


def half_hour(start):
    return Slot(start, start + timedelta(minutes=30))


class SchedulingAcceptance(TestCase):
    def setUp(self):
        factory = load_factory()
        if factory is None:
            self.skipTest("Set CALCOM_TEST_ADAPTER=module:factory after implementing the workflow")
        self.calcom = CalComStub()
        self.environment = Environment(self.calcom)
        self.driver = factory(self.environment)
        if close := getattr(self.driver, "close", None):
            self.addCleanup(close)

    def assertStatus(self, turn, status):
        self.assertEqual(turn.status, status, turn.message)
        self.assertTrue(turn.task_id)
        self.assertTrue(turn.message.strip())

    def assertHandoff(self, turn, source, target):
        self.assertIn((source, target, turn.task_id), {
            (handoff.source, handoff.target, handoff.task_id) for handoff in turn.handoffs
        })
        self.assertTrue(all(handoff.task_id == turn.task_id for handoff in turn.handoffs))

    def assertLocalSlot(self, slot, expected):
        for actual, instant in ((slot.start, expected.start), (slot.end, expected.end)):
            self.assertEqual(actual.timestamp(), instant.timestamp())
            self.assertEqual(actual.utcoffset(), instant.astimezone(ZoneInfo(ZONE)).utcoffset())

    def requested(self, method, path):
        return [request for request in self.calcom.requests if (request.method, request.path) == (method, path)]

    def set_original(self):
        self.calcom.bookings["original-uid"] = booking("original-uid", utc(5, 15))

    def assertCancelledOnce(self):
        self.assertEqual(len(self.requested("POST", CANCEL)), 1)
        self.assertEqual(self.calcom.bookings["original-uid"]["status"], "cancelled")

    def check_missing(self, omitted_text, field, answer):
        self.calcom.slots = [half_hour(utc(5, 15))]
        first = self.driver.turn(CREATE.replace(omitted_text, ""))
        self.assertStatus(first, "awaiting_input")
        self.assertEqual(first.missing_fields, (field,))
        self.assertHandoff(first, "booking", "confirmation")
        self.assertEqual(self.calcom.writes, [])
        result = self.driver.turn(answer)
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertHandoff(result, "confirmation", "booking")
        self.assertEqual(len(self.calcom.writes), 1)
        self.assertEqual(result.bookings[0].uid, next(iter(self.calcom.bookings)))
        self.assertLocalSlot(result.bookings[0].slot, self.calcom.slots[0])

    def test_create_asks_for_email_and_resumes_same_task(self):
        self.check_missing(", alex@example.test", "attendee.email", "alex@example.test")

    def test_create_asks_for_time_zone(self):
        self.check_missing(" America/New_York", "attendee.timeZone", "America/New_York")

    def test_create_asks_for_required_event_field(self):
        self.check_missing(" The topic is Planning.", "bookingFieldsResponses.topic", "Planning")

    def test_create_reports_provider_pending_status(self):
        self.calcom.slots = [half_hour(utc(5, 15))]
        self.calcom.created_status = "pending"
        result = self.driver.turn(CREATE)
        self.assertStatus(result, "completed")
        self.assertEqual(result.bookings[0].status, "pending")
        self.assertRegex(result.message.casefold(), r"pending|awaiting|waiting")
        self.assertEqual(len(self.calcom.writes), 1)

    def test_unavailable_slot_requires_selection_and_is_rechecked(self):
        alternative = half_hour(utc(5, 16))
        self.calcom.slots = [alternative]
        first = self.driver.turn(CREATE)
        self.assertStatus(first, "awaiting_input")
        self.assertEqual(first.slots, (alternative,))
        self.assertEqual(first.time_zone, ZONE)
        self.assertEqual(self.calcom.writes, [])
        before_answer = len(self.calcom.requests)
        result = self.driver.turn("Book the 11 AM option.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        [write] = self.calcom.writes
        write_index = self.calcom.requests.index(write)
        self.assertTrue(any(request.path == SLOTS for request in self.calcom.requests[before_answer:write_index]))
        self.assertEqual(parse_time(write.body["start"]), alternative.start)

    def test_create_recovers_from_slot_race_with_fresh_alternatives(self):
        alternative = half_hour(utc(5, 16))
        self.calcom.slots = [half_hour(utc(5, 15))]

        def lose_slot(request):
            self.calcom.slots = [alternative]
            return self.calcom.respond(request)

        self.calcom.script("POST", BOOKINGS, lose_slot)
        result = self.driver.turn(CREATE)
        self.assertStatus(result, "awaiting_input")
        self.assertEqual(result.slots, (alternative,))
        self.assertHandoff(result, "booking", "recovery")
        self.assertEqual(len(self.calcom.writes), 1)
        self.assertEqual(self.calcom.bookings, {})

    def test_list_paginates_deduplicates_and_preserves_overlap_and_local_time(self):
        crossing = booking("crosses-start", utc(2, 11, 15))
        later = booking("later", utc(2, 13), status="pending")
        self.calcom.bookings = {item["uid"]: item for item in (
            later,
            booking("cancelled", utc(2, 12, 30), status="cancelled"),
            crossing,
            booking("ends-at-start", utc(2, 11)),
            booking("starts-at-end", utc(2, 15, 30)),
        )}
        self.calcom.page_size = 2

        def add_duplicate(request):
            response = self.calcom.respond(request)
            if request.query.get("cursor") is not None:
                response.payload["data"].append(later)
            return response

        self.calcom.script("GET", BOOKINGS, add_duplicate, add_duplicate)
        result = self.driver.turn("List all bookings on November 2, 2030 from 7:30 AM to 11:30 AM America/New_York.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.time_zone, ZONE)
        self.assertEqual([item.uid for item in result.bookings], ["crosses-start", "later"])
        self.assertEqual(result.bookings[1].status, "pending")
        self.assertLocalSlot(result.bookings[0].slot, half_hour(utc(2, 11, 15)))
        self.assertLocalSlot(result.bookings[1].slot, half_hour(utc(2, 13)))
        pages = self.requested("GET", BOOKINGS)
        self.assertGreaterEqual(len(pages), 3)
        self.assertNotIn("cursor", pages[0].query)
        self.assertTrue(all(request.query.get("cursor") for request in pages[1:]))
        self.assertTrue(all("status" not in request.query for request in pages))
        self.assertEqual(self.calcom.writes, [])

    def test_list_retries_failed_page_and_reports_incomplete_after_exhaustion(self):
        self.calcom.page_size = 1
        self.calcom.bookings = {
            "first": booking("first", utc(5, 15)),
            "second": booking("second", utc(5, 16)),
        }
        unavailable = error(503, "Service unavailable")
        self.calcom.script("GET", BOOKINGS, self.calcom.respond, unavailable, unavailable, unavailable)
        result = self.driver.turn(f"List bookings for {RANGE}.")
        self.assertStatus(result, "incomplete")
        self.assertHandoff(result, "retrieval", "recovery")
        self.assertEqual(
            [request.query.get("cursor") for request in self.requested("GET", BOOKINGS)],
            [None, "page:1", "page:1", "page:1"],
        )
        self.assertEqual(self.calcom.writes, [])

    def test_vague_get_returns_current_booking_details(self):
        self.set_original()
        result = self.driver.turn("When and where is my call with Alex on November 5, America/New_York?")
        self.assertStatus(result, "completed")
        self.assertHandoff(result, "triage", "retrieval")
        self.assertTrue(self.requested("GET", ORIGINAL))
        [detail] = result.bookings
        self.assertEqual(detail.uid, "original-uid")
        self.assertEqual(detail.status, "accepted")
        self.assertEqual(detail.title, "Consultation with Alex Rivera")
        self.assertEqual(detail.attendee_name, "Alex Rivera")
        self.assertEqual(detail.host_name, "Morgan Lee")
        self.assertEqual(detail.event_type_id, self.environment.event_type_id)
        self.assertEqual(detail.meeting_url, "https://example.test/meeting")
        self.assertLocalSlot(detail.slot, half_hour(utc(5, 15)))
        self.assertEqual(self.calcom.writes, [])

    def test_ambiguous_get_asks_which_booking(self):
        self.set_original()
        self.calcom.bookings["other-uid"] = booking("other-uid", utc(5, 16), numeric_id=102)
        result = self.driver.turn("Show details for my call with Alex on November 5, America/New_York.")
        self.assertStatus(result, "awaiting_input")
        self.assertEqual({item.uid for item in result.candidates}, {"original-uid", "other-uid"})
        self.assertEqual(result.bookings, ())
        self.assertHandoff(result, "retrieval", "confirmation")

    def test_ambiguous_get_resumes_retrieval_after_choice(self):
        self.set_original()
        self.calcom.bookings["other-uid"] = booking("other-uid", utc(5, 16), numeric_id=102)
        first = self.driver.turn("Show details for my call with Alex on November 5, America/New_York.")
        self.assertStatus(first, "awaiting_input")
        result = self.driver.turn("The 11 AM one.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertHandoff(result, "confirmation", "retrieval")
        self.assertEqual([item.uid for item in result.bookings], ["other-uid"])
        self.assertEqual(self.calcom.writes, [])

    def test_unclear_intent_is_clarified_then_routed_through_triage(self):
        first = self.driver.turn("I need help with my schedule.")
        self.assertStatus(first, "awaiting_input")
        self.assertHandoff(first, "triage", "confirmation")
        result = self.driver.turn(f"List bookings for {RANGE}.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertHandoff(result, "confirmation", "triage")
        self.assertHandoff(result, "triage", "retrieval")
        self.assertEqual(self.calcom.writes, [])

    def test_vague_cancel_resolves_and_rereads_before_write(self):
        self.set_original()
        result = self.driver.turn("Cancel my call with Alex on November 5, America/New_York.")
        self.assertStatus(result, "completed")
        self.assertHandoff(result, "triage", "booking")
        self.assertHandoff(result, "booking", "retrieval")
        self.assertHandoff(result, "retrieval", "booking")
        self.assertCancelledOnce()
        cancel_index = self.calcom.requests.index(self.calcom.writes[0])
        self.assertIn(("GET", ORIGINAL), [
            (request.method, request.path) for request in self.calcom.requests[:cancel_index]
        ])
        self.assertEqual(result.bookings[0].status, "cancelled")

    def test_ambiguous_cancel_only_cancels_selected_occurrence(self):
        self.set_original()
        self.calcom.bookings["other-uid"] = booking("other-uid", utc(5, 16), numeric_id=102)
        first = self.driver.turn("Cancel my call with Alex on November 5, America/New_York.")
        self.assertStatus(first, "awaiting_input")
        self.assertEqual({item.uid for item in first.candidates}, {"original-uid", "other-uid"})
        self.assertEqual(self.calcom.writes, [])
        prior = len(self.calcom.requests)
        result = self.driver.turn("Cancel the 11 AM one.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual([request.path for request in self.calcom.writes], [f"{BOOKINGS}/other-uid/cancel"])
        cancel_index = self.calcom.requests.index(self.calcom.writes[0])
        self.assertIn(("GET", f"{BOOKINGS}/other-uid"), [
            (request.method, request.path) for request in self.calcom.requests[prior:cancel_index]
        ])
        self.assertEqual(self.calcom.bookings["original-uid"]["status"], "accepted")

    def test_get_and_cancel_report_no_match_after_search(self):
        for action in ("Show details for", "Cancel"):
            with self.subTest(action=action):
                result = self.driver.turn(f"{action} my call with Alex on November 5, America/New_York.")
                self.assertStatus(result, "not_found")
                self.assertEqual(result.bookings, ())
                self.assertEqual(self.calcom.writes, [])

    def test_cancel_reports_already_cancelled_without_second_write(self):
        self.calcom.bookings["original-uid"] = booking("original-uid", utc(5, 15), status="cancelled")
        result = self.driver.turn("Cancel booking original-uid.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.bookings[0].status, "cancelled")
        self.assertRegex(result.message.casefold(), r"already|previously")
        self.assertEqual(self.calcom.writes, [])

    def test_reschedule_checks_slot_cancels_and_creates_replacement(self):
        self.set_original()
        self.calcom.slots = [half_hour(utc(5, 16))]

        def create_after_cancel(request):
            self.assertEqual(self.calcom.bookings["original-uid"]["status"], "cancelled")
            return self.calcom.respond(request)

        self.calcom.script("POST", BOOKINGS, create_after_cancel)
        result = self.driver.turn(MOVE)
        self.assertStatus(result, "completed")
        self.assertEqual([request.path for request in self.calcom.writes], [CANCEL, BOOKINGS])
        cancel_index = self.calcom.requests.index(self.calcom.writes[0])
        self.assertTrue(any(request.path == SLOTS for request in self.calcom.requests[:cancel_index]))
        self.assertEqual(result.original.uid, "original-uid")
        self.assertEqual(result.original.status, "cancelled")
        self.assertLocalSlot(result.original.slot, half_hour(utc(5, 15)))
        self.assertLocalSlot(result.replacement.slot, half_hour(utc(5, 16)))
        self.assertIn(result.replacement.uid, self.calcom.bookings)
        self.assertNotEqual(result.replacement.uid, result.original.uid)

    def test_reschedule_keeps_original_when_destination_is_unavailable(self):
        self.set_original()
        self.calcom.slots = [half_hour(utc(5, 17))]
        result = self.driver.turn(MOVE)
        self.assertStatus(result, "awaiting_input")
        self.assertEqual(result.slots, tuple(self.calcom.slots))
        self.assertEqual(self.calcom.writes, [])
        self.assertEqual(self.calcom.bookings["original-uid"]["status"], "accepted")

    def test_reschedule_to_same_slot_keeps_existing_booking(self):
        self.set_original()
        result = self.driver.turn("Move booking original-uid to November 5 at 10 AM America/New_York.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.original.uid, "original-uid")
        self.assertEqual(result.original.status, "accepted")
        self.assertIsNone(result.replacement)
        self.assertEqual(self.calcom.writes, [])

    def test_partial_reschedule_resumes_creation_without_second_cancel(self):
        self.set_original()
        self.calcom.slots = [half_hour(utc(5, 16))]

        def lose_destination(request):
            self.calcom.slots = [half_hour(utc(5, 17))]
            return self.calcom.respond(request)

        self.calcom.script("POST", BOOKINGS, lose_destination)
        first = self.driver.turn(MOVE)
        self.assertStatus(first, "awaiting_input")
        self.assertCancelledOnce()
        self.assertHandoff(first, "booking", "recovery")
        self.assertEqual(first.original.status, "cancelled")
        self.assertIsNone(first.replacement)
        self.assertEqual(first.slots, tuple(self.calcom.slots))
        self.assertRegex(first.message.casefold(), r"cancel[l]?ed|cancellation")
        result = self.driver.turn("Book the noon option.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual([request.path for request in self.calcom.writes], [CANCEL, BOOKINGS, BOOKINGS])
        self.assertLocalSlot(result.replacement.slot, half_hour(utc(5, 17)))
        self.assertNotEqual(self.calcom.writes[1].body["metadata"], self.calcom.writes[2].body["metadata"])

    def check_lost_response(self, operation, prompt, blocked_read):
        def commit_without_response(request):
            response = self.calcom.respond(request)
            self.assertIn(response.status, (200, 201))
            self.calcom.unavailable_reads.add(blocked_read)
            raise TimeoutError("Response lost after dispatch")

        self.calcom.script("POST", operation, commit_without_response)
        first = self.driver.turn(prompt)
        self.assertStatus(first, "recoverable")
        self.assertHandoff(first, "booking", "recovery")
        self.assertEqual([request.path for request in self.calcom.writes], [operation])
        self.calcom.unavailable_reads.clear()
        result = self.driver.resume(first.task_id)
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual([request.path for request in self.calcom.writes], [operation])
        return result

    def test_lost_create_response_is_reconciled_on_resume(self):
        self.calcom.slots = [half_hour(utc(5, 15))]
        result = self.check_lost_response(BOOKINGS, CREATE, BOOKINGS)
        self.assertEqual([result.bookings[0].uid], list(self.calcom.bookings))

    def test_lost_cancel_response_is_reconciled_on_resume(self):
        self.set_original()
        result = self.check_lost_response(CANCEL, "Cancel booking original-uid.", ORIGINAL)
        self.assertEqual(result.bookings[0].uid, "original-uid")
        self.assertEqual(result.bookings[0].status, "cancelled")
        self.assertCancelledOnce()

    def test_cancel_readback_reports_an_independent_change_without_a_second_write(self):
        self.set_original()

        def change_booking(request):
            self.calcom.bookings["original-uid"] = booking("original-uid", utc(5, 16))
            raise TimeoutError("Provider outcome unknown")

        self.calcom.script("POST", CANCEL, change_booking)
        result = self.driver.turn("Cancel booking original-uid.")
        self.assertStatus(result, "recoverable")
        self.assertHandoff(result, "recovery", "confirmation")
        self.assertIn("changed booking details", result.message)
        self.assertEqual(result.bookings[0].slot.start, utc(5, 16))
        self.assertEqual(len(self.calcom.writes), 1)

    def test_definitively_rate_limited_create_returns_to_booking_after_recheck(self):
        self.calcom.slots = [half_hour(utc(5, 15))]
        self.calcom.script("POST", BOOKINGS, error(429, "Rate limited"))
        result = self.driver.turn(CREATE)
        self.assertStatus(result, "completed")
        self.assertHandoff(result, "recovery", "booking")
        first, second = self.calcom.writes
        self.assertEqual(first.body["metadata"], second.body["metadata"])
        self.assertIn(SLOTS, [
            request.path for request in self.calcom.requests[
                self.calcom.requests.index(first) + 1 : len(self.calcom.requests) - 1
            ]
        ])
        self.assertEqual(len(self.calcom.bookings), 1)

    def test_unresolved_create_does_not_claim_unrelated_matching_booking_or_retry_post(self):
        self.calcom.slots = [half_hour(utc(5, 15))]

        def timeout_with_unrelated_booking(request):
            self.calcom.bookings["unrelated-uid"] = booking("unrelated-uid", utc(5, 15))
            raise TimeoutError("Provider outcome unknown")

        self.calcom.script("POST", BOOKINGS, timeout_with_unrelated_booking)
        first = self.driver.turn(CREATE)
        self.assertStatus(first, "recoverable")
        result = self.driver.resume(first.task_id)
        self.assertStatus(result, "recoverable")
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual(result.bookings, ())
        self.assertEqual([request.path for request in self.calcom.writes], [BOOKINGS])

    def test_availability_returns_ordered_slots_that_entirely_fit(self):
        first, last = half_hour(utc(5, 15)), half_hour(utc(5, 16, 30))
        self.calcom.slots = [half_hour(utc(5, 16, 45)), last, first, half_hour(utc(5, 17))]
        result = self.driver.turn(f"When is a Consultation available on {RANGE}?")
        self.assertStatus(result, "completed")
        self.assertHandoff(result, "triage", "retrieval")
        self.assertEqual(result.time_zone, ZONE)
        self.assertEqual(result.slots, (first, last))
        for actual, expected in zip(result.slots, (first, last)):
            self.assertLocalSlot(actual, expected)
        self.assertEqual(self.calcom.writes, [])

    def test_availability_empty_is_a_successful_response(self):
        result = self.driver.turn(f"Show available Consultation slots for {RANGE}.")
        self.assertStatus(result, "completed")
        self.assertEqual(result.slots, ())
        self.assertEqual(result.time_zone, ZONE)
        self.assertTrue(self.requested("GET", SLOTS))

    def test_availability_provider_failure_is_recoverable(self):
        self.calcom.unavailable_reads.add(SLOTS)
        result = self.driver.turn(f"Show available Consultation slots for {RANGE}.")
        self.assertStatus(result, "recoverable")
        self.assertHandoff(result, "retrieval", "recovery")
        self.assertEqual(len(self.requested("GET", SLOTS)), 3)
        self.assertEqual(self.calcom.writes, [])

    def test_repeated_dst_time_is_clarified_before_create(self):
        self.calcom.slots = [half_hour(utc(3, 5, 30)), half_hour(utc(3, 6, 30))]
        prompt = CREATE.replace("November 5, 2030 at 10 AM", "November 3, 2030 at 1:30 AM")
        first = self.driver.turn(prompt)
        self.assertStatus(first, "awaiting_input")
        self.assertEqual(self.calcom.writes, [])
        result = self.driver.turn("The second 1:30 AM, after the clocks go back (UTC-05:00).")
        self.assertStatus(result, "completed")
        self.assertEqual(result.task_id, first.task_id)
        self.assertEqual(len(self.calcom.writes), 1)
        self.assertEqual(parse_time(self.calcom.writes[0].body["start"]), utc(3, 6, 30))
