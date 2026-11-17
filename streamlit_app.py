"""Streamlit chat and scheduling details for the Cal.com workflow."""

import logging
from dataclasses import dataclass
from uuid import uuid4

import streamlit as st

from scheduling_agents.outcomes import FIELD_NAMES, booking_label, interval_label
from streamlit_runtime import ConfigurationError, WorkflowBridge, get_workflow
from workflow_context import Account, Booking, Outcome


STATUS_LABELS = {
    "active": "In progress",
    "awaiting_input": "Needs a reply",
    "completed": "Complete",
    "not_found": "No match",
    "incomplete": "Partial results",
    "recoverable": "Paused",
    "failed": "Unable to complete",
}
BOOKING_STATUSES = {"accepted": "Confirmed", "pending": "Awaiting host confirmation"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Action:
    message: str
    reply_to: str | None = None
    question_key: str | None = None
    resume_id: str | None = None
    booking_reference: str | None = None


def render_booking(booking: Booking, time_zone: str | None, account: Account, title: str | None = None):
    with st.container(border=True):
        st.text(title or booking.title or "Booking")
        if title and booking.title:
            st.text(booking.title)
        st.caption(interval_label(booking.slot, time_zone or booking.timeZone))
        status = BOOKING_STATUSES.get(booking.status, booking.status.capitalize())
        st.text(f"{status} · {booking.calcom_uid}")
        event = account.event(booking.event_type_id)
        if event:
            st.text(f"Event type · {event.name}")
        if booking.name:
            st.text(f"Attendee · {booking.name}")
        if booking.attendee_email:
            st.text(booking.attendee_email)
        if booking.host_name:
            st.text(f"Host · {booking.host_name}")
        url = booking.meeting_url or booking.location
        has_link = url.startswith(("https://", "http://"))
        if has_link:
            st.link_button("Open meeting link", url)
        if booking.location and (booking.location != url or not has_link):
            st.text(booking.location)


def render_details(outcome: Outcome, workflow: WorkflowBridge):
    st.caption(STATUS_LABELS[outcome.status])
    if outcome.status != "completed" or not (outcome.bookings or outcome.slots or outcome.original):
        st.text(outcome.message.splitlines()[0])
    if outcome.time_zone:
        st.caption(f"Time zone · {outcome.time_zone}")
    if outcome.missing_fields:
        st.markdown("**What’s needed**")
        for name in outcome.missing_fields:
            st.text(FIELD_NAMES.get(name, name.removeprefix("bookingFieldsResponses.")))

    if outcome.question_key and (outcome.candidates or outcome.slots):
        labels = [booking_label(item, outcome.time_zone) for item in outcome.candidates] or [
            interval_label(slot, outcome.time_zone) for slot in outcome.slots
        ]
        heading = "Matching bookings" if outcome.candidates else "Available times"
        selected = st.selectbox(
            heading, range(len(labels)), index=None, placeholder="Choose an option",
            format_func=labels.__getitem__, key=f"choice-{outcome.question_key}",
        )
        st.button(
            "Send selection", width="stretch", disabled=selected is None,
            on_click=submit_choice, args=(workflow, outcome),
        )
    elif outcome.slots:
        with st.expander(f"Available times ({len(outcome.slots)})", expanded=True):
            for slot in outcome.slots:
                st.text(interval_label(slot, outcome.time_zone))

    if outcome.original:
        render_booking(outcome.original, outcome.time_zone, workflow.account, "Original booking")
    if outcome.replacement:
        render_booking(outcome.replacement, outcome.time_zone, workflow.account, "Replacement booking")
    shown = {item.calcom_uid for item in (outcome.original, outcome.replacement) if item}
    bookings = [item for item in outcome.bookings if item.calcom_uid not in shown]
    if bookings:
        with st.expander(f"Bookings ({len(bookings)})", expanded=True):
            for booking in bookings:
                render_booking(booking, outcome.time_zone, workflow.account)

    if outcome.status in ("recoverable", "incomplete"):
        st.button(
            "Resume request", width="stretch", key=f"resume-{outcome.task_id}",
            on_click=submit, args=(workflow, Action("Resume this request", resume_id=outcome.task_id)),
        )
    with st.expander("Task reference"):
        st.code(outcome.task_id, language=None)


def render_sidebar(workflow: WorkflowBridge) -> Outcome | None:
    with st.sidebar:
        st.header("Schedule details")
        outcomes = st.session_state.outcomes
        reply = None
        if outcomes:
            def label(task_id: str) -> str:
                message, result = outcomes[task_id]
                summary = " ".join(message.split())
                if len(summary) > 54:
                    summary = f"{summary[:51]}…"
                return f"{STATUS_LABELS[result.status]} · {summary}"

            task_id = st.selectbox(
                "Request", list(reversed(outcomes)), format_func=label, key="active_request",
            )
            _, outcome = outcomes[task_id]
            render_details(outcome, workflow)
            if outcome.question_key and st.checkbox(
                "Direct the next chat reply to this question", key=f"reply-{outcome.question_key}",
            ):
                reply = outcome
        else:
            st.caption("Booking details and available times appear here after your first request.")

        with st.expander("Calendar settings", expanded=not outcomes):
            if workflow.account.default_time_zone:
                st.caption(f"Default time zone · {workflow.account.default_time_zone}")
            for event in workflow.account.event_types:
                durations = event.supported_durations or (event.duration_minutes,)
                minutes = ", ".join(str(duration) for duration in durations)
                st.text(f"{event.name} · {minutes} min")
        return reply


def chat_action(message: str, reply: Outcome | None) -> Action:
    words = message.split()
    if len(words) in (2, 3) and words[0].casefold() == "resume":
        return Action(message, resume_id=words[1], booking_reference=words[2] if len(words) == 3 else None)
    if reply:
        return Action(message, reply.task_id, reply.question_key)
    return Action(message)


def submit(workflow: WorkflowBridge, action: Action):
    state = st.session_state
    if state.pending_turn is not None:
        return
    if action.resume_id:
        future = workflow.resume(action.resume_id, state.conversation_id, booking_reference=action.booking_reference)
    else:
        future = workflow.turn(
            action.message, state.conversation_id, reply_to=action.reply_to, question_key=action.question_key,
            turn_id=uuid4().hex,
        )
    state.messages.append(("user", action.message))
    state.pending_turn = (action, future)


def submit_choice(workflow: WorkflowBridge, outcome: Outcome):
    selected = st.session_state.get(f"choice-{outcome.question_key}")
    if selected is not None:
        submit(workflow, Action(f"Option {selected + 1}", outcome.task_id, outcome.question_key))


def submit_chat(workflow: WorkflowBridge, reply: Outcome | None):
    message = (st.session_state.get("scheduling_message") or "").strip()
    if message:
        submit(workflow, chat_action(message, reply))


def complete_turn():
    state = st.session_state
    action, future = state.pending_turn
    with st.chat_message("assistant"):
        with st.spinner("Checking your schedule…"):
            try:
                outcome = future.result()
            except Exception as exc:
                logger.warning("Scheduling request did not return a result (%s)", type(exc).__name__)
                message = "The request didn’t return a result. Check the booking status before submitting it again."
            else:
                message = outcome.message
                request = state.outcomes.get(outcome.task_id, (action.message, None))[0]
                state.outcomes[outcome.task_id] = (request, outcome)
                state.active_request = outcome.task_id
            state.messages.append(("assistant", message))
            state.pending_turn = None
        st.text(message)


def main():
    st.set_page_config(page_title="Scheduling desk", page_icon="📅", initial_sidebar_state="auto")
    st.html("""
        <style>
        .stMainBlockContainer {max-width: 54rem; padding-top: 3.5rem;}
        .st-key-introduction {border-left: 3px solid #48775e; padding-left: 1.15rem; margin-bottom: 1rem;}
        [data-testid="stText"] {white-space: pre-wrap; overflow-wrap: anywhere;}
        </style>
    """)
    with st.container(key="introduction"):
        st.title("Scheduling desk")
        st.caption("Book, move, or cancel meetings and check availability on your Cal.com calendar.")

    try:
        workflow = get_workflow()
    except ConfigurationError as exc:
        st.info(str(exc))
        return

    state = st.session_state
    if "conversation_id" not in state:
        state.conversation_id = uuid4().hex
        state.messages = []
        state.outcomes = {}
        state.pending_turn = None
    if not state.messages:
        st.subheader("What would you like to schedule?")
        available, booked = st.columns(2)
        event_name = workflow.account.event_types[0].name
        available.button(
            "Check availability", width="stretch", on_click=submit,
            args=(workflow, Action(f"Show available times for {event_name} next week.")),
        )
        booked.button(
            "List bookings", width="stretch", on_click=submit,
            args=(workflow, Action("List my bookings for the next seven days.")),
        )

    for role, message in state.messages:
        with st.chat_message(role):
            st.text(message)
    if state.pending_turn:
        complete_turn()
    reply = render_sidebar(workflow)
    placeholder = "Reply to the selected question" if reply else "Ask about a booking or available time"
    st.chat_input(placeholder, key="scheduling_message", on_submit=submit_chat, args=(workflow, reply))


main()
