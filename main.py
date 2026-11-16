"""Run a terminal conversation with the Cal.com scheduling agents."""

import argparse
import asyncio
import json
import os

from dotenv import load_dotenv

from workflow import SchedulingWorkflow
from workflow_context import Account, EventType


def configured_account() -> Account:
    config = json.loads(os.environ["CALCOM_EVENT_TYPES"])
    if not isinstance(config, list) or not config or not all(isinstance(event, dict) for event in config):
        raise ValueError("CALCOM_EVENT_TYPES must be a nonempty JSON array of event definitions")
    events = tuple(
        EventType(
            id=event["id"], name=event["name"], duration_minutes=event["duration_minutes"],
            required_fields=tuple(event.get("required_fields", ())),
            supported_durations=tuple(event.get("supported_durations", ())),
            location_required=event.get("location_required", False), locations=event.get("locations", {}),
        )
        for event in config
    )
    return Account(
        events, default_time_zone=os.environ.get("CALCOM_TIME_ZONE"),
        default_attendee_name=os.environ.get("CALCOM_ATTENDEE_NAME"),
        default_attendee_email=os.environ.get("CALCOM_ATTENDEE_EMAIL"),
    )


async def conversation(workflow: SchedulingWorkflow, initial: str | None, interactive: bool):
    try:
        await run_conversation(workflow, initial, interactive)
    finally:
        await workflow.close()


async def run_conversation(workflow: SchedulingWorkflow, initial: str | None, interactive: bool):
    message = initial
    while True:
        if message is None:
            try:
                message = input("Scheduling request> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
        if message.casefold() in ("quit", "exit"):
            return
        if not message:
            message = None
            continue
        words = message.split()
        if len(words) in (2, 3) and words[0].casefold() == "resume":
            reference = words[2] if len(words) == 3 else None
            outcome = await workflow.resume(words[1], "terminal", booking_reference=reference)
        else:
            outcome = await workflow.turn(message, "terminal")
        print(outcome.message)
        if not interactive:
            return
        message = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?", help="Optional initial scheduling request")
    parser.add_argument("--once", action="store_true", help="Return after one turn")
    args = parser.parse_args()
    load_dotenv()
    missing = [
        key for key in ("OPENAI_API_KEY", "CALCOM_API_KEY", "CALCOM_EVENT_TYPES")
        if not os.environ.get(key)
    ]
    if missing:
        parser.error("Set " + ", ".join(missing) + " in the shell or .env")
    try:
        workflow = SchedulingWorkflow(
            configured_account(), os.environ["CALCOM_API_KEY"],
            model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
        )
    except (KeyError, TypeError, ValueError):
        parser.error("Check CALCOM_EVENT_TYPES and CALCOM_TIME_ZONE against the configuration in README.md")
    asyncio.run(conversation(workflow, args.message, not args.once))


if __name__ == "__main__":
    main()
