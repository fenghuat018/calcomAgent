# Context data plan

[Scheduling specification](../spec.md#shared-context-data-classes) · [Tool creation plan](tool-creation-plan.md) · [Workflow plan](workflow-plan.md)

## Define the shared records

Use Python data classes in `workflow_context.py`. Use validated, structured fields so the application can enforce allowed state transitions and serialize the records predictably.

| Record | Fields and role |
| --- | --- |
| `WorkflowContext` | `tasks: dict[str, Task]` scoped to the current authenticated conversation. Carry the active task ID and references to trusted runtime dependencies such as the clock, account configuration, and Cal.com client. |
| `Task` | `intent: str`, `target_booking: Booking` or `None`, and `api_errors: dict[str, APIError]`. Track the current step, status, and revision, validated request inputs, normalized range and display zone, search scope, and latest verified outcome. Store booking candidates, available slots, pending question and continuation, and the current list cursor and accumulated results as needed. |
| `Booking` | `booking_uid: int` from Cal.com's numeric `id`, `calcom_uid: str` from its opaque `uid`, `name: str`, `timeZone: str`, and `slots: dict` with UTC `start` and `end`. Keep the event type ID, status, title, authorized attendee and host details, and applicable booking responses and location data. |
| `APIError` | `request_method: str`, `request_url: str`, `request_body: dict`, `error_code: int`, and `error_message: str`. Use an empty request body for GETs, and `0` when there is no HTTP response. Redact sensitive fields and associate each error with the operation or query, page or input revision, and attempt. |

Keep the original in `target_booking` during a reschedule. Store the validated replacement draft and the resulting replacement booking separately. Represent each write with its stable operation ID, requested target or slot, input revision, dispatch state, attempt count, next permitted retry time, and verified result or failure. A create operation also stores its expected correlation marker. Preserve the facts needed to rebuild a permitted request from validated inputs and to tell whether a read result is fresh for that request.

## In-memory state and access

1. Define typed states for active work, awaiting input, complete, not found, incomplete, recoverable, and terminal failure. Track write progress independently as planned, in flight, verified applied, verified unapplied, or uncertain. Use one checked update path for task and operation transitions.
2. Keep one `WorkflowContext` and one in-memory SDK session per authenticated account and conversation in the workflow service. Reuse them across calls. Implement the SDK's `Session` protocol over an in-memory list for conversation history. Save transitions with their task revision to prevent stale updates. Context and history are scoped to the lifetime of the process.
3. Save pending questions with a field or choice set and a continuation role and step. Store the confirmed booking UID or slot, the input revision it applies to, and the evidence required to verify it. Reject stale answers or results after a material change.
4. Serialize conversation turns and coordinate mutations for the same original occurrence across tasks with in-process locking. Claim the operation and mark it `in flight` before sending the HTTP request. Update the same task with the verified result or uncertainty afterward. If a later run within the process finds an `in flight` or uncertain operation, route it to reconciliation before another write.
5. Supply the same context object to all agents during a run. Expose role-specific projections such as candidate summaries or failure classifications. Store approved booking information needed to continue the work. Keep credentials in runtime configuration and exclude raw provider failures and personal booking payloads from operational logs and traces.

## Verify the context contract

Verify that a follow-up keeps the original task ID and that an unrelated request gets a different one. After a lost write response, resume the task in the same process and check that the relevant operation state and reschedule records are available. Verify that a recovered result is scoped to the account and conversation and that re-entering a completed operation returns the saved outcome. See the [test adapter contract](../test/README.md#adapter-contract).
