# calcomAgent

A scheduling assistant with Streamlit and terminal interfaces. Five agents handle intake, booking changes, retrieval, clarification, and recovery. See the [scheduling specification](spec.md) and [agent creation plan](plan/agent-creation-plan.md).

## Setup

Use Python 3.10 or newer.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Set the following values in the shell or in a local `.env` file.

```dotenv
OPENAI_API_KEY=your_openai_key
CALCOM_API_KEY=your_calcom_key
CALCOM_EVENT_TYPES='[{"id":42,"name":"Consultation","duration_minutes":30,"required_fields":["topic"]}]'
```

Use the IDs and booking fields of event types already configured in the authorized Cal.com account. Each event definition can also specify `supported_durations` as a list of minutes, `location_required` as a boolean, and `locations` as a mapping from display names to Cal.com location objects.

Optional defaults are `CALCOM_TIME_ZONE` (an IANA zone), `CALCOM_ATTENDEE_NAME`, and `CALCOM_ATTENDEE_EMAIL`. Set `OPENAI_MODEL` to override `gpt-5-mini`.

## Run

### Streamlit

```sh
.venv/bin/python -m streamlit run streamlit_app.py
```

Open [the local scheduling desk](http://127.0.0.1:8501). Use chat for scheduling requests and the sidebar to review details, choose a booking or time, and resume paused requests. Each browser session holds its own conversation in memory. The local server uses the single Cal.com account configured above.

### Terminal

```sh
.venv/bin/python main.py
.venv/bin/python main.py "List bookings tomorrow in America/New_York"
```

The terminal stays open for answers to scheduling questions. To resume a recoverable task, enter `resume <task-id>` in the same process. After a restart, enter `resume <task-id> <booking-uid-or-id>` to fetch the current booking from Cal.com. Use `--once` for a single turn.

## Verify offline

```sh
CALCOM_TEST_ADAPTER=test.adapter:make_driver .venv/bin/python -m unittest discover -s test -v
```

The acceptance tests use the SDK runner, a controlled interpreter, and a Cal.com HTTP stub. See the [test adapter contract](test/README.md).

## Verify online

```sh
CALCOM_TEST_ADAPTER=test.adapter:make_online_driver .venv/bin/python -m unittest discover -s test -v
.venv/bin/python -m unittest test.live_calcom -v
```

The acceptance suite sends synthetic prompts to the configured OpenAI model and uses controlled Cal.com responses for scheduling scenarios. The second command checks the configured Cal.com account with authenticated reads. Set `CALCOM_LIVE_EVENT_TYPE_ID` to choose the event type for the availability check.
