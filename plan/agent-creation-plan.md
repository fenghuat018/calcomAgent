# Agent creation plan

[Scheduling specification](../spec.md) · [Tool creation plan](tool-creation-plan.md) · [Context data plan](context-data-plan.md)

## Agent definitions

Put the five definitions in `scheduling_agents/` so `agents` resolves to the installed Python SDK. Compose the agents in one factory. Create the instances first, then wire their `handoff()` edges before the first `Runner.run` call. Type all five agents and their tools against `WorkflowContext`.

| Agent | Instructions to give the agent | Tools to attach |
| --- | --- | --- |
| Triage | Identify the user's intent and attach the known inputs to the active task. Route writes to Booking and reads to Retrieval. Route unclear intent to Confirmation. | Read the current request and save the parsed intent and inputs. |
| Booking | Validate the requested mutation. Ask Retrieval to resolve and refresh an existing booking or check a destination slot. Ask Confirmation for a missing field or a choice. For reschedule, create the replacement only after verified cancellation. Route API failures to Recovery. | Read booking workflow state, save the validated mutation plan, and call `create_booking` and `cancel_booking`. |
| Retrieval | Search only the authorized scope. Resolve matching candidates, obtain current details, return complete booking lists and available slots, or perform the requested reconciliation read. Report ambiguity, incompleteness, and failure as distinct outcomes. | Read the saved search or verification request, save its result, and call `list_bookings`, `get_booking`, and `get_availability`. |
| Confirmation | Ask the specific question the task requires. Present candidate bookings and alternative slots with their local dates, times, and time zone. Record the answer against the saved question and re-ask if it remains unresolved. | Read the pending question and its choices. Save the question and validated answer. |
| Recovery | Use the recorded operation and observed provider evidence to select a permitted recovery action. Budget retries, honor delays, request reconciliation for uncertain writes, and preserve verified partial outcomes. | Read failure and checkpoint summaries. Record the permitted retry, reconciliation, or continuation. |

## Handoff allowlist

| Source | Destination | Condition |
| --- | --- | --- |
| Triage | Booking | Intent is create, cancel, or reschedule. |
| Triage | Retrieval | Intent is list bookings, get booking, or get availability. |
| Triage | Confirmation | Intent is unclear. |
| Booking | Retrieval | A booking needs resolution, a fresh read, or an availability check. |
| Booking | Confirmation | A required field, booking choice, or slot choice is unresolved. |
| Booking | Recovery | A Cal.com write failed or its outcome needs verification after an error. |
| Retrieval | Booking | A saved read result is ready for a Booking continuation. |
| Retrieval | Recovery | A read failed or a reconciliation result is ready for Recovery. |
| Retrieval | Confirmation | A required read input or booking choice is unresolved. |
| Confirmation | Triage | The user clarified the scheduling intent. |
| Confirmation | Booking | The user supplied the validated inputs or choice needed for a write task. |
| Confirmation | Retrieval | The user supplied the validated inputs or choice needed for a read task. |
| Recovery | Retrieval | A read may be retried, an uncertain write needs reconciliation, or a rejected slot needs fresh alternatives. |
| Recovery | Booking | Verified state permits the exact write retry or continuation. |
| Recovery | Confirmation | The task needs a corrected input or a selection from fresh options. |

## Wiring and verification

1. Add a short purpose and a completion condition to each agent's instructions. Generate the final response from the saved outcome. Enforce prerequisites in local handlers so model instructions cannot authorize tools, alter a provider UID, or approve an uncertain write.
2. Give each allowed edge a typed payload with `task_id`, a reason, and a saved result or error key. Include the key for the pending question or saved continuation when relevant. Let `is_enabled` expose only the edges and tools useful at the current phase. In `on_handoff`, revalidate the active task, role, destination, and referenced record. Return a specialist's result by saving it and explicitly handing back control where needed.
3. Attach individual workflow state tools with role-specific read and update permissions. Recovery can authorize a bounded retry from recorded evidence, and only Booking can execute that write. Booking gets read results through Retrieval. Confirmation receives the projected question and choices.
4. Verify the actual agent definitions against the allowlist and exercise the handoff paths in the [acceptance suite](../test/test_acceptance.py). Capture SDK events and confirm that all handoffs for one request retain the same `task_id`, including the continuation after a clarification turn.
