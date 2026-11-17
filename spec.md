# Cal.com scheduling agent specification

## Scope

Provide a conversational scheduling workflow for creating, listing, finding, cancelling, and rescheduling bookings, and for finding available slots. Use the repository's Python OpenAI Agents SDK (`openai-agents==0.24.0`) for agent orchestration and Cal.com API v2 as the source of booking and availability data. Use the existing `CALCOM_API_KEY` environment variable and a configured, authorized Cal.com account with one or more existing event types. Create and reschedule one booking at a time. Booking lists include individual occurrences that Cal.com returns. The supplied schedules reference describes configuration of event-type working hours. The six scheduling operations use the Bookings and Slots endpoints. [Cal.com schedule configuration](https://cal.com/docs/api-reference/v2/schedules/create-a-schedule)

## Agents and handoffs

The user interface starts a new request at Triage. All agent-to-agent transitions use the SDK's `handoff()` and transfer control to the destination. A specialist that needs another specialist's result gets control back through an explicit handoff if there is work left for it to do. The agent that reaches an outcome returns it to the user interface. [OpenAI Agents SDK handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)

| Agent | Responsibilities and tools | Handoff destinations |
| --- | --- | --- |
| Triage | Create or resume a task and identify its intent. Workflow state tools only. | Booking for create, cancel, or reschedule. Retrieval for list, get, or availability. Confirmation when the intent is unclear. |
| Booking | Own writes. Cal.com tools `create_booking` and `cancel_booking`. Reschedule coordinates those two tools. | Retrieval to resolve a booking, check availability, or verify a booking. Confirmation for missing inputs, ambiguous choices, or slot selection. Recovery after a Cal.com failure. |
| Retrieval | Own reads. Cal.com tools `list_bookings`, `get_booking`, and `get_availability`. Resolve vague references through those reads. | Booking with a resolved booking, availability result, or verified state when Booking is awaiting it. Recovery with a reconciliation result when Recovery is awaiting it, or an error when a read fails. Confirmation for missing or ambiguous inputs. |
| Confirmation | Ask a focused question or present dated, time-zoned options and record the user's answer. Workflow state tools only. | Triage when intent is resolved. Booking or Retrieval to resume the pending step. |
| Recovery | Classify recorded failures, manage retry budgets, and determine the next safe step. Workflow state tools only. | Retrieval for reconciliation and read retries. Booking for an authorized write retry or continuation. Confirmation when user input or a new slot is needed. |

Every agent uses `WorkflowContext` as its SDK context type. Pass the same object to `Runner.run` and access it in tools and callbacks through `RunContextWrapper.context`. Before handing off, store validated outputs or errors on the task. Include `task_id`, a reason, and the result or error key in a typed handoff payload. Validate the referenced record, destination, and task phase in `on_handoff`. Expose the relevant task fields through a scoped workflow state tool so the receiving model can read them. Keep the full context in application code. Gate Cal.com tool execution on the saved task phase, selected booking or slot, and account scope. [OpenAI Agents SDK handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)

When Confirmation needs the user, save the question, expected fields or candidate choices, and the continuation step, then end the run with an `awaiting_input` outcome. On reply, reuse the same task and SDK conversation session and start a new run at Confirmation for a reply to that question. Start an unrelated request at Triage as a new task. If the reply could address multiple pending tasks, ask which request it refers to. Keep task checkpoints and SDK conversation history in application memory across turns for the lifetime of the process. [Running agents and continuing conversations](https://developers.openai.com/api/docs/guides/agents/running-agents)

## Shared context data classes

| Data class | Required fields and semantics |
| --- | --- |
| `WorkflowContext` | `tasks` (`dict[str, Task]`), keyed by stable task ID and scoped to the authenticated conversation. |
| `Task` | `intent` (`str`), one of `create_booking`, `list_bookings`, `cancel_booking`, `reschedule_booking`, `get_booking`, or `get_availability` after triage. `target_booking` (`Booking` or `None`), unset until the original booking is resolved or a new booking is created. For reschedules it continues to identify the original. `api_errors` (`dict[str, APIError]`), keyed by a stable query or operation key that identifies the step and, where relevant, the page or input revision. |
| `Booking` | `booking_uid` (`int`), populated from Cal.com's numeric `data.id` or the list item `id`. `calcom_uid` (`str`), the opaque Cal.com `uid` used in `bookingUid` path parameters. `name` (`str`), the primary attendee's name. `timeZone` (`str`), the attendee's IANA time zone. `slots` (`dict`), containing the booking's `start` and `end` as UTC ISO 8601 timestamps. Also retain `event_type_id`, provider status, title, and authorized attendee and location details as needed for retrieval or recreation. [Cal.com booking representation](https://cal.com/docs/api-reference/v2/bookings/get-a-booking) |
| `APIError` | `request_method` (`str`), `request_url` (`str`), `request_body` (`dict`), `error_code` (`int`), and `error_message` (`str`). Use an empty body for GETs and code `0` for transport failures without an HTTP response. Preserve the HTTP status for response failures and classify the failure separately. Record redacted request and error details. |

Each task also holds its status and current step, validated intent inputs, search range and display time zone, candidate bookings and available slots, pending question and continuation destination, and the latest verified result. The replacement booking and its requested details have separate fields. Record per-step operation IDs, attempt counts, last failure category, and provider outcomes. A write checkpoint records the request identity before dispatch and the verified result after it completes. Serialize turns per task and mutations affecting the same original booking. Treat a repeated turn or tool call for an already completed operation as a request for its saved outcome.

## Cal.com API v2

Use `https://api.cal.com` with server-side `Authorization: Bearer <CALCOM_API_KEY>` and the endpoint-specific `cal-api-version` header. Set `Content-Type: application/json` for POSTs. Validate the expected response body as well as the HTTP response status. Keep credentials, personal booking details, and raw provider errors out of operational logs and traces. Only pass task-relevant data to model-facing tools. [Cal.com API v2 authentication](https://cal.com/docs/api-reference/v2/introduction)

| Tool | Endpoint and version | Request and result |
| --- | --- | --- |
| `create_booking` | `POST /v2/bookings`, `2026-02-25` | Send UTC `start`, `eventTypeId`, `attendee` with `name`, `email`, and `timeZone` for the standard email-based workflow, plus event-specific fields such as `bookingFieldsResponses` or `location` when required. Send `lengthInMinutes` for a selected supported duration on variable-length events. Include a stable operation ID in booking `metadata` for reconciliation. Read the returned IDs, status, and actual times from `data`. [Create a booking](https://cal.com/docs/api-reference/v2/bookings/create-a-booking) |
| `list_bookings` | `GET /v2/bookings`, `2026-05-01` | Apply appropriate scope and reliable identifying filters such as `eventTypeId` or `attendeeEmail`. Omit `status` for listings across time and filter returned booking statuses locally by default. Read `data` and follow `pagination.nextCursor` as `cursor` until `pagination.hasMore` is false. Preserve the search filters across pages. [Get all bookings](https://cal.com/docs/api-reference/v2/bookings/get-all-bookings) |
| `get_booking` | `GET /v2/bookings/{bookingUid}`, `2026-02-25` | Use `calcom_uid` in the path. Read current status, UTC bounds, event and attendee details, and location or meeting URL when present. [Get a booking](https://cal.com/docs/api-reference/v2/bookings/get-a-booking) |
| `cancel_booking` | `POST /v2/bookings/{bookingUid}/cancel`, `2026-02-25` | Use the resolved `calcom_uid` and send the user's `cancellationReason` or a generic default. For a recurring booking, use the occurrence UID for a single occurrence and leave subsequent bookings outside the operation. Verify cancellation from the response or a follow-up read. [Cancel a booking](https://cal.com/docs/api-reference/v2/bookings/cancel-a-booking) |
| `get_availability` | `GET /v2/slots`, `2024-09-04` | Send `eventTypeId`, UTC ISO 8601 `start` and `end`, and `format=range`. Use `timeZone` as needed for display and `duration` for a selected supported variable length. For a reschedule preflight, pass `bookingUidToReschedule` if the original booking should be excluded from its own busy-time calculation. Normalize the returned date-indexed slot ranges. An empty slots map is a successful empty result. [Get available slots](https://cal.com/docs/api-reference/v2/slots/get-available-time-slots-for-an-event-type) |

Reschedule uses the specified cancel-then-create sequence. Cal.com also documents a dedicated reschedule endpoint. The workflow records its own link between the two bookings because it issues independent cancellation and creation requests. [Cal.com reschedule endpoint](https://cal.com/docs/api-reference/v2/bookings/reschedule-a-booking)

## Input, matching, and time ranges

- Resolve dates and relative phrases against the application's current time in the user's explicit or saved IANA time zone. Ask when the zone, date, or local time is genuinely ambiguous, including repeated or nonexistent times at daylight-saving transitions. Convert to UTC for comparisons and API requests. Display dates, start and end times, and the applicable time zone in responses.
- Require `start < end` for a range and use half-open bounds `[start, end)`. A date-only range expands in the user's time zone. List a booking if `booking.start < range.end` and `booking.end > range.start`, including a booking that spans either boundary. Cal.com's `afterStart` and `beforeEnd` enforce different bounds. Avoid sending them for an overlap search unless a broader provider window can be proven to contain all overlaps, then apply the exact filter locally. Return available slots whose entire meeting fits in the requested range. [Booking date filters](https://cal.com/docs/api-reference/v2/bookings/get-all-bookings)
- Resolve an explicit Cal.com UID directly. Resolve a numeric booking ID by matching it against authorized list results before using the corresponding Cal.com UID. For phrases such as “my call with Alex tomorrow,” combine authorized attendee, title, time, event type, and conversation references to search. A unique match must satisfy the supplied clues. For multiple plausible matches or weak evidence, Confirmation asks for a distinguishing detail or offers numbered choices with title and local time. For zero matches after a complete meaningful search, report the search scope and ask for another clue when the user can provide one. A failed or incomplete lookup has its own outcome and supplies insufficient evidence for a mutation.
- Use trusted account defaults or known attendee information when they resolve a field uniquely. Ask only for unresolved required inputs. After a user chooses a booking, re-read it by UID and check its identity, status, and material details before a cancellation or reschedule. If the selected booking has materially changed, present the updated details through Confirmation.

## Intent behavior

### Create booking

Collect the event type, attendee identity and time zone, required event fields, and one chosen start time. A time window can be used to find options. Retrieval checks the requested slot or window. If the chosen slot is unavailable, return fresh, ordered alternatives for the same event type and duration within the requested window. For an exact-time request without a wider window, search the rest of that local day and state that scope. Ask the user to pick an alternative or supply a different range if there are none. Recheck a selected alternative on the next turn. Treat a slot-related create rejection after a successful availability check as a race and use the same alternatives flow. Report the saved booking's actual time, UID, and provider status. Describe `pending` as awaiting host confirmation.

### List bookings

Accept a range and optional filters. Return every authorized booked occurrence that overlaps the range across all provider pages, ordered by start time and deduplicated by occurrence UID. Include past and upcoming bookings and label pending bookings. Include cancelled or rejected entries when specifically requested and label their status. Each entry includes the title or attendee, interval, time zone, status, and reference. Report an empty result after the complete search. If a page fails, resume from its cursor and surface an incomplete result if retries are exhausted. The interface may paginate a complete result for display without dropping entries.

### Get booking

Resolve the reference, then read the current detail by UID. Return the booking reference, title, authorized attendee and host details, event type, time interval and zone, status, and location or meeting link when present. Disambiguate before returning detailed information when more than one booking matches.

### Cancel booking

Accept an explicit cancellation request even if the booking is described informally. Resolve and re-read the target using Retrieval. A clear single match and explicit cancellation intent authorize cancellation of that booking. Ask the user to identify the booking if the result is ambiguous. Report a verified already-cancelled status as such. On a verified new cancellation, identify the booking and say it was cancelled. If no target can be found, report that outcome without issuing a write.

### Reschedule booking

1. Resolve and re-read the original booking. Collect the replacement start and any new required inputs. Reuse the original event type, attendee information, supported duration, and applicable booking fields unless the user requested a change. Validate the complete replacement payload and check the destination slot before cancelling. A request for the existing slot with identical details returns the current booking. Offer alternatives through Confirmation if the destination is unavailable.
2. Record the original booking and replacement plan. Cancel the original booking and verify the result. Proceed to replacement creation only once cancellation is verified for this task. If the original has changed independently before this operation, route back to Confirmation with the current state.
3. Record verified cancellation in the task, then create the replacement. On success, report the old and new times, new UID, and replacement status. If creation fails after cancellation, Recovery resumes at replacement creation when safe. If the slot was lost, give fresh alternatives and tell the user the original was cancelled. An unresolved write outcome leaves the task recoverable and is described explicitly. Recreating the old slot requires the user's choice and is a new booking subject to current availability.

### Get availability

Resolve the event type, range, display time zone, and duration if needed. Return all slots Cal.com reports for that request after filtering to the range, ordered chronologically with start and end times. If no slots fit, report the checked range and the empty result. An available slot can be selected in a later reply and revalidated when used for a booking.

## Exceptions and recovery

Cal.com tools catch HTTP, transport, and response-decoding failures and record an `APIError` with the associated operation and attempt. Preserve the application checkpoint before handing off to Recovery. Use a bounded SDK turn limit. If the SDK run fails before a handoff can occur, the application records the failure and can start Recovery with the saved task and step when resumption is possible. Report a pending reconciliation when a write was in flight at the time of the failure.

| Failure | Recovery behavior |
| --- | --- |
| Temporary read failure, including timeout, `429`, or retryable `5xx` | Retry the failed read up to two additional times with capped exponential backoff and jitter. Honor `Retry-After` when provided. If the requested delay exceeds the current interaction's wait budget, save the earliest retry time and return a recoverable status. |
| Temporary write failure with a verified lack of side effects | Apply the same bounded retry policy only after verifying the request was never sent or was definitively rejected before applying. Revalidate the target or slot. Treat lost responses and uncertain provider failures as ambiguous write outcomes. |
| Missing or invalid user input | Route to Confirmation with the exact field or choice required. Classify other `400` and `422` responses by provider details. Correct invalid input before another attempt. |
| Unavailable slot or booking conflict | Identify the rejected slot from provider details. Request fresh slots from Retrieval and send the result to Confirmation. Treat unrelated `409` responses according to their actual cause. |
| `401`, `403`, invalid configuration, or a missing resource | Surface the actionable connection or configuration issue, or resolve a stale booking reference again when appropriate. A missing resource alone is insufficient to verify cancellation. |
| Ambiguous cancel result | Read back the resolved UID. Mark the step complete if Cal.com confirms cancellation for that booking and there is no evidence of an independent change. If the record links to a replacement created by another reschedule, show the current state through Confirmation. Retry only when the original is verified active, the intended cancellation is still valid, and the prior request is known to have been rejected or to have finished without applying. Otherwise keep reconciliation pending. |
| Ambiguous create result, including a lost response | Search via Retrieval by event type, attendee and time, then verify an exact operation-ID match in booking metadata. A unique correlated booking completes the step. If the prior request is known to have failed without creating a booking, a bounded retry may reuse the operation ID and recheck the slot. If the outcome remains uncertain, return a recoverable status and preserve the checkpoint. The metadata is a correlation marker and provides no provider-side deduplication. |
| Permanent or exhausted failure | Return the verified state, a request reference, and any concrete action needed from the user. After a partial reschedule, distinguish a cancelled original, a verified replacement, and an unresolved replacement outcome. |

Use the task's recorded request and retry budget for recovery. After a terminal result or an ambiguous mutation, gate further writes on the saved state. An in-flight operation found on a later run within the same process enters reconciliation before another write. A process restart starts with empty task state. For an old task reference, ask for booking details and check Cal.com before treating the request as a new action. Confirming a different replacement slot requires a new operation ID and a definitive outcome for the previous create attempt. Log task ID, operation ID, step, status, and attempt without storing sensitive payloads in logs.

## Acceptance criteria

| Scenario | Expected result |
| --- | --- |
| Create request lacks attendee email, time zone, or an event-required answer | Confirmation asks for the missing values. The reply resumes the same task and a valid available slot is created once. |
| Requested slot is already taken or becomes unavailable during creation | Retrieval returns fresh alternatives, Confirmation asks for a choice, and no alternative is booked without that choice. |
| Listing spans several pages and includes a booking crossing the start of the range | All applicable entries are returned once in local time. An unrecovered page error is reported as incomplete. |
| Vague get or cancel request has one, several, or zero matches | A reliable single match is used. Several matches trigger a choice. Zero matches produce a scoped response without a write. |
| Reschedule succeeds | The original cancellation is verified before the replacement is created. The response includes both times and the new reference. |
| Cancellation succeeds but replacement fails | The task records the partial outcome and Recovery resumes from replacement creation. The original cancellation is issued once. |
| Create or cancel response is lost and recovery resumes in a later run within the same process | The task is recovered from its in-memory checkpoint, the provider outcome is reconciled, and an unresolved outcome never triggers a duplicate POST. |
| Availability has slots, has no slots, or the provider fails | Results include only fitting slots, a verified empty result, or a recoverable failure, respectively. |
| A local time repeats at a daylight-saving transition | Confirmation resolves which time was meant before a write. |
| A workflow step requires another agent's tools | The current agent uses an authorized handoff. Shared task state stays in memory across that handoff and through a clarification turn. |
