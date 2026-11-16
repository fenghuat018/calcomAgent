# Tool creation plan

[Scheduling specification](../spec.md) · [Context data plan](context-data-plan.md) · [Agent creation plan](agent-creation-plan.md)

## Package layout

| Proposed file | Responsibility |
| --- | --- |
| `tools/calcom_client.py` | Send Cal.com requests, select the endpoint version, validate responses, and classify failures. Accept an injectable HTTP transport. |
| `tools/booking.py` | Expose the Booking Agent's `create_booking` and `cancel_booking` SDK function tools. Validate write authorization and save operation checkpoints. |
| `tools/retrieval.py` | Expose the Retrieval Agent's `list_bookings`, `get_booking`, and `get_availability` SDK function tools. Normalize provider records and checkpoint paginated reads. |

Keep the transport behind these five tools. Use `RunContextWrapper[WorkflowContext]` to load the active task and its authenticated account scope. Tool inputs should reference validated task data, such as a saved slot, booking UID, or read cursor. Build headers, URLs, and provider payloads in application code.

## Endpoint contracts

| Tool | Method, path, and `cal-api-version` | Behavior |
| --- | --- | --- |
| `create_booking` | `POST /v2/bookings` · `2026-02-25` | Send the chosen UTC `start`, `eventTypeId`, attendee `name`, `email`, and `timeZone`, and applicable event-required fields. Include `lengthInMinutes` for a selected variable duration and save the stable create-operation ID in `metadata`. Normalize the returned booking and retain its actual status and times. |
| `cancel_booking` | `POST /v2/bookings/{bookingUid}/cancel` · `2026-02-25` | Send the verified occurrence's opaque UID and optional cancellation reason. Use single-occurrence semantics. Return a verified cancellation or an outcome requiring read-back. |
| `list_bookings` | `GET /v2/bookings` · `2026-05-01` | Apply authorized account and verified search filters. Omit `status` for cross-status searches. Apply overlap bounds locally because `afterStart` and `beforeEnd` can exclude valid overlaps. Follow `pagination.nextCursor` while `hasMore` is true. Save the next cursor and accumulated rows in the task after each valid page so Recovery can resume at the failed cursor. Reject repeated or missing continuation cursors when the provider reports more pages. |
| `get_booking` | `GET /v2/bookings/{bookingUid}` · `2026-02-25` | Use the opaque UID. Return the current booking or a typed missing or error result. Apply access checks before exposing any details. |
| `get_availability` | `GET /v2/slots` · `2024-09-04` | Send `eventTypeId`, UTC `start` and `end`, and `format=range`. Include the selected `duration` for variable-length events and `bookingUidToReschedule` during preflight when the original should be excluded. Normalize the date-indexed response into ordered intervals that fit the input range. An empty map is a successful empty result. |

Reschedule uses `cancel_booking` followed by `create_booking`. The workflow owns their sequence and checkpointing across the two operations. Cal.com event types and their working-hour schedules are provisioned before use. See the [endpoint references in the specification](../spec.md#calcom-api-v2).

## Build and verify

1. Configure `https://api.cal.com`, the server-side bearer token from `CALCOM_API_KEY`, request timeouts, and JSON POST headers. Use the same request path for real and stubbed transports. Disable automatic HTTP retries so the workflow can apply the recorded retry policy.
2. Validate the HTTP status, provider envelope, and endpoint-specific response data. Map Cal.com's numeric `id` to `booking_uid` and its string `uid` to `calcom_uid`. Normalize times as UTC. Preserve HTTP status for response errors and use `0` for transport failures with no response.
3. Convert expected failures into a recorded `APIError` and a typed outcome with an error key. Distinguish a verified rejection or pre-dispatch failure from a timeout, invalid response, or server failure whose write outcome is unknown. Preserve `Retry-After` when available. Classify slot conflicts from provider details and record only redacted diagnostics.
4. Enforce the saved task phase and account scope in tool handlers. Before a write, verify the selected UID or slot and claim the operation in the in-memory task. Save a verified result before reporting success. Expose `pending` as awaiting host confirmation. Treat an uncertain write response as a request for reconciliation by Retrieval through Recovery.
5. Exercise the transport through the [Cal.com test stub](../test/support.py). Check endpoint headers and payloads, slot bounds, cursor continuation after a failed page, cancel-by-UID, and write response loss. Use the same client to exercise both successful HTTP responses and failures in the [acceptance suite](../test/test_acceptance.py).
