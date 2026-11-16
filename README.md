# calcomAgent

A Python starter for the [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/quickstart). The example runs one scheduling assistant in the terminal and includes a separate Cal.com API check.

## Setup

Use Python 3.10 or newer. The commands below use Python 3.13.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Add `OPENAI_API_KEY` to `.env` at the repo root using a key from the [OpenAI API keys page](https://platform.openai.com/api-keys). Set `OPENAI_MODEL` if you want to override the default `gpt-5-mini`. Shell environment variables take precedence over `.env` values.

If your IDE flags `from agents import ...` as unresolved, select `.venv/bin/python` as the project's existing Python interpreter. The SDK's package name is `openai-agents` and its import name is `agents`.

Run the connectivity check or pass your own prompt.

```sh
.venv/bin/python main.py
.venv/bin/python main.py "Draft a note asking to reschedule a 30-minute meeting."
```

Set `CALCOM_API_KEY` in `.env` to verify Cal.com separately. The check uses the read-only [profile endpoint](https://cal.com/docs/api-reference/v2/me/get-my-profile) and prints only the check result.

```sh
.venv/bin/python main.py --check-calcom
```
