"""Verify Streamlit reruns and sidebar actions against the scheduling workflow."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from streamlit_runtime import WorkflowBridge
from test.adapter import make_driver
from test.support import CalComStub, Environment, Slot, utc
from test.test_acceptance import BOOKINGS, CREATE


class StreamlitTests(TestCase):
    def setUp(self):
        self.calcom = CalComStub()
        driver = make_driver(Environment(self.calcom))
        bridge = WorkflowBridge(driver.workflow)
        self.addCleanup(bridge.close)
        workflow_patch = patch("streamlit_runtime.get_workflow", return_value=bridge)
        workflow_patch.start()
        self.addCleanup(workflow_patch.stop)
        script = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        self.app = AppTest.from_file(str(script)).run()
        self.assertFalse(self.app.exception)

    def chat(self, message):
        self.app.chat_input[0].set_value(message).run()
        self.assertFalse(self.app.exception)

    def sidebar_button(self, label):
        return next(button for button in self.app.sidebar.button if button.label == label)

    def test_clarification_and_booking_survive_widget_reruns(self):
        self.calcom.slots = [Slot(utc(5, 15), utc(5, 15, 30))]
        self.chat(CREATE.replace(", alex@example.test", ""))
        self.assertIn("Needs a reply", [item.value for item in self.app.sidebar.caption])
        self.assertEqual(self.calcom.writes, [])
        self.app.sidebar.checkbox[0].check().run()
        self.chat("alex@example.test")
        self.assertIn("Complete", [item.value for item in self.app.sidebar.caption])
        [uid] = self.calcom.bookings
        self.assertIn(f"Confirmed · {uid}", [item.value for item in self.app.sidebar.text])
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertEqual(len(self.calcom.writes), 1)
        self.assertEqual(len(self.app.chat_message), 4)

    def test_sidebar_slot_selection_replies_to_the_saved_question(self):
        self.calcom.slots = [Slot(utc(5, 16), utc(5, 16, 30))]
        self.chat(CREATE)
        choices = next(item for item in self.app.sidebar.selectbox if item.label == "Available times")
        choices.set_value(0).run()
        self.sidebar_button("Send selection").click().run()
        self.assertFalse(self.app.exception)
        self.assertIn("Complete", [item.value for item in self.app.sidebar.caption])
        [write] = self.calcom.writes
        self.assertEqual(write.body["start"], "2030-11-05T16:00:00Z")

    def test_sidebar_resume_reconciles_without_resubmitting_create(self):
        self.calcom.slots = [Slot(utc(5, 15), utc(5, 15, 30))]

        def lose_response(request):
            self.calcom.respond(request)
            self.calcom.unavailable_reads.add(BOOKINGS)
            raise TimeoutError("Response lost after dispatch")

        self.calcom.script("POST", BOOKINGS, lose_response)
        self.chat(CREATE)
        self.assertIn("Paused", [item.value for item in self.app.sidebar.caption])
        self.calcom.unavailable_reads.clear()
        self.sidebar_button("Resume request").click().run()
        self.assertFalse(self.app.exception)
        self.assertIn("Complete", [item.value for item in self.app.sidebar.caption])
        self.assertEqual(len(self.calcom.writes), 1)
