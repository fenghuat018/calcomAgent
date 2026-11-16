"""Optional read-only checks against the configured Cal.com account."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest import IsolatedAsyncioTestCase

from dotenv import load_dotenv

from tools.calcom_client import CalCom, https_request
from workflow_context import Slot


def clock():
    return datetime.now(timezone.utc)


class CalComLiveReads(IsolatedAsyncioTestCase):
    def setUp(self):
        load_dotenv()
        self.key = os.environ.get("CALCOM_API_KEY")
        if not self.key:
            self.fail("CALCOM_API_KEY is required for live Cal.com checks")
        self.calcom = CalCom(self.key, clock)

    async def test_booking_list_and_existing_detail(self):
        bookings, _ = await self.calcom.list_bookings({"limit": 1}, None, set())
        self.assertLessEqual(len(bookings), 1)
        if bookings:
            listed = bookings[0]
            detail = await self.calcom.get_booking(listed.calcom_uid)
            self.assertTrue((listed.booking_uid, listed.calcom_uid) == (detail.booking_uid, detail.calcom_uid))

    async def test_slot_ranges_for_an_existing_event_type(self):
        configured = os.environ.get("CALCOM_LIVE_EVENT_TYPE_ID")
        if configured:
            event_id = int(configured)
        else:
            response = await asyncio.to_thread(
                https_request, "GET", "/v2/event-types",
                headers={"Authorization": f"Bearer {self.key}", "cal-api-version": "2026-06-12"},
                query={}, body=None,
            )
            self.assertEqual(response.status, 200)
            self.assertIsInstance(response.payload, dict)
            self.assertTrue(response.payload.get("status") == "success")
            events = response.payload.get("data")
            if not events:
                self.skipTest("The account has no event type to check")
            event_id = events[0]["id"]
        now = clock()
        bounds = Slot(now, now + timedelta(days=3))
        slots = await self.calcom.get_availability({"eventTypeId": event_id}, bounds)
        self.assertEqual(slots, tuple(sorted(slots)))
        self.assertTrue(all(bounds.start <= slot.start < slot.end <= bounds.end for slot in slots))
