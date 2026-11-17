"""Keep the scheduling workflow on one event loop across Streamlit reruns."""

import asyncio
import atexit
import os
from concurrent.futures import Future
from threading import Thread

import streamlit as st
from dotenv import load_dotenv

from main import configured_account
from workflow import SchedulingWorkflow
from workflow_context import Outcome


class ConfigurationError(ValueError):
    """A setup issue that can be displayed without exposing credentials."""


class WorkflowBridge:
    def __init__(self, workflow: SchedulingWorkflow):
        self._workflow = workflow
        self.account = workflow.account
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._serve, name="scheduling-workflow", daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._workflow.close())
            self._loop.close()

    def turn(self, message: str, conversation_id: str, **kwargs) -> Future[Outcome]:
        return asyncio.run_coroutine_threadsafe(
            self._workflow.turn(message, conversation_id, **kwargs), self._loop,
        )

    def resume(self, task_id: str, conversation_id: str, **kwargs) -> Future[Outcome]:
        return asyncio.run_coroutine_threadsafe(
            self._workflow.resume(task_id, conversation_id, **kwargs), self._loop,
        )

    def close(self):
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=3)


@st.cache_resource(show_spinner=False)
def get_workflow() -> WorkflowBridge:
    load_dotenv()
    missing = [
        key for key in ("OPENAI_API_KEY", "CALCOM_API_KEY", "CALCOM_EVENT_TYPES")
        if not os.environ.get(key)
    ]
    if missing:
        raise ConfigurationError("Set these values in the shell or .env before starting Streamlit: " + ", ".join(missing))
    try:
        account = configured_account()
    except (KeyError, TypeError, ValueError):
        raise ConfigurationError("Check CALCOM_EVENT_TYPES and CALCOM_TIME_ZONE in the setup instructions.") from None
    bridge = WorkflowBridge(SchedulingWorkflow(
        account, os.environ["CALCOM_API_KEY"], model=os.environ.get("OPENAI_MODEL") or "gpt-5-mini",
    ))
    atexit.register(bridge.close)
    return bridge
