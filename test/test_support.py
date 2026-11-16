"""Verify the fixture's pagination, ranges, and response-loss simulation."""

from unittest import TestCase

from test.support import EVENT_ID, TOKEN, ZONE, CalComStub, Slot, booking, iso, utc


class CalComStubTests(TestCase):
    def setUp(self):
        self.calcom = CalComStub()

    def request(self, method, path, version, **kwargs):
        return self.calcom.request(method, path, headers={
            "Authorization": f"Bearer {TOKEN}",
            "cal-api-version": version,
            "Content-Type": "application/json",
        }, **kwargs)

    def test_list_pages_keep_opaque_cursor_and_filter(self):
        self.calcom.page_size = 1
        self.calcom.bookings = {
            "first": booking("first", utc(5, 15)),
            "different": booking("different", utc(5, 16), name="Taylor"),
            "last": booking("last", utc(5, 17)),
        }
        query = {"attendeeName": "Alex"}
        response = self.request("GET", "/v2/bookings", "2026-05-01", query=query)
        self.assertEqual(response.payload["data"][0]["uid"], "first")
        self.assertTrue(response.payload["pagination"]["hasMore"])
        query["cursor"] = response.payload["pagination"]["nextCursor"]
        response = self.request("GET", "/v2/bookings", "2026-05-01", query=query)
        self.assertEqual(response.payload["data"][0]["uid"], "last")
        self.assertEqual(response.payload["pagination"], {"hasMore": False, "nextCursor": None})
        self.assertEqual(self.calcom.requests[0].query, {"attendeeName": "Alex"})

    def test_slots_stub_preserves_end_beyond_requested_window_for_client_filtering(self):
        self.calcom.slots = [Slot(utc(5, 16, 45), utc(5, 17, 15)), Slot(utc(5, 17), utc(5, 17, 30))]
        response = self.request("GET", "/v2/slots", "2024-09-04", query={
            "eventTypeId": str(EVENT_ID), "format": "range", "start": iso(utc(5, 15)), "end": iso(utc(5, 17)),
        })
        self.assertEqual(response.payload["data"], {"2030-11-05": [
            {"start": iso(utc(5, 16, 45)), "end": iso(utc(5, 17, 15))},
        ]})

    def test_create_can_commit_even_when_transport_loses_response(self):
        self.calcom.slots = [Slot(utc(5, 15), utc(5, 15, 30))]

        def commit_then_timeout(request):
            self.assertEqual(self.calcom.respond(request).status, 201)
            raise TimeoutError("Lost response")

        self.calcom.script("POST", "/v2/bookings", commit_then_timeout)
        body = {
            "start": iso(utc(5, 15)), "eventTypeId": EVENT_ID,
            "attendee": {"name": "Alex Rivera", "email": "alex@example.test", "timeZone": ZONE},
            "bookingFieldsResponses": {"topic": "Planning"}, "metadata": {"operationId": "create-123"},
        }
        with self.assertRaises(TimeoutError):
            self.request("POST", "/v2/bookings", "2026-02-25", body=body)
        [created] = self.calcom.bookings.values()
        self.assertEqual(created["metadata"], body["metadata"])
        self.assertEqual(len(self.calcom.writes), 1)

    def test_stub_rejects_wrong_endpoint_version(self):
        with self.assertRaisesRegex(AssertionError, "Wrong API version"):
            self.request("GET", "/v2/bookings", "2026-02-25")
