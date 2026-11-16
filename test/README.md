# Scheduling acceptance tests

`test_acceptance.py` exercises the workflows in [spec.md](../spec.md) through their conversation entry point. It checks structured outcomes and user-facing messages alongside recorded Cal.com requests and SDK handoffs. The Cal.com stub covers endpoint headers, paginated booking data, availability, conflicts, and mutations whose responses are lost. The fixture tests can run before the scheduling application exists.

From the repository root, run the suite with Python 3.10 or newer.

```sh
python -m unittest discover -s test -v
```

The application scenarios are skipped until a test adapter is configured. An invalid adapter setting fails the run. Once the application exists, expose a factory in an importable module and run the full suite with its dotted path.

```sh
CALCOM_TEST_ADAPTER=your_adapter_module:make_driver python -m unittest discover -s test -v
```

## Adapter contract

- `make_driver(environment)` returns a driver implementing `turn(message)` and `resume(task_id)`. Construct one workflow instance per scenario and reuse it for both methods so task and conversation state stay in memory. `turn` sends the original text through the application's conversation entry point. `resume` recovers the existing in-memory task without a new scheduling instruction.
- Use the environment's fixed clock, conversation ID, API key, and existing 30-minute Consultation event with required field `topic`. Leave the test conversation without a default attendee email or time zone. Inject a no-op retry delay so the bounded retries can run offline and deterministically.
- Connect the application's Cal.com client to `environment.calcom.request`. Pass the actual method, URL path without its query string, headers, decoded query values, and JSON body. Translate `Response.status`, `payload`, and `headers` back into the client's HTTP response. Translate `TimeoutError` into the client's ambiguous transport timeout. The client must receive the same requests and responses it would use through its normal network transport.
- Capture handoffs from the application's SDK execution. Return their source and target using `triage`, `booking`, `retrieval`, `confirmation`, and `recovery`. Include the actual task ID associated with each handoff in that turn. A controlled SDK model fixture can supply deterministic tool selections while exercising real tools, handoffs, state transitions, and recovery.
- Map application output to `Turn` in `support.py`. Preserve the actual user message and task ID. Use the normalized statuses `completed`, `awaiting_input`, `not_found`, `incomplete`, and `recoverable` for the corresponding outcomes. A successful empty result is `completed` with an empty collection. Use canonical Cal.com field paths for `missing_fields`. Return normalized booking details or selection candidates, and preserve original and replacement state in their respective fields for reschedules. Use the actual display time zone and time-zone-aware datetimes for returned booking intervals and slots.

Keep the adapter limited to configuration, transport injection, invocation, and observation. The application supplies the scheduling decisions and outcomes. For an offline run, use an SDK model fixture and send all Cal.com traffic to the stub. These tests verify the workflow under controlled inputs. Live-model interpretation and compatibility with a particular Cal.com account require separate verification.
