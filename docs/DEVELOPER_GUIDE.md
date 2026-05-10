# Developer Guide

This guide explains how the code is organized and how the main runtime flows work. It is written for a junior developer who needs to understand the project quickly and make safe changes.

## 1. What This Project Does

This repository contains two connected hospitality systems:

1. A voicebot that answers SIP calls in Asterisk, talks to OpenAI Realtime, collects hotel service requests, transfers calls, sends email transcripts, posts Rainbow messages, and can create wake-up calls through the hospitality API.
2. A guest web application backend and frontend for Rainbow Hospitality services, including guest authentication, room service, wake-up calls, and captive portal support.

The voicebot runtime is the most active part of the project. The key files are:

- `agi/voice_assistant_eagi.py`: Asterisk EAGI entrypoint and call loop.
- `agi/openai_realtime_client.py`: OpenAI Realtime API client, prompt, tools, and turn processing.
- `agi/call_session.py`: call state, language detection, deterministic transfer/end-call logic.
- `agi/audio_utils.py`: audio capture, conversion, WAV writing, and audio quality checks.
- `api/app/main.py`: FastAPI backend for guest auth, room service proxy, and wake-up call creation.
- `agi/rainbow_service_request_notifier.js`: sends service requests into Rainbow bubbles using the Rainbow Node SDK.
- `tests/`: unit tests for the voicebot behavior.

## 2. Runtime Architecture

The voicebot runs inside the `asterisk` Docker service.

Call path:

1. A SIP caller reaches Asterisk.
2. Asterisk routes the call to extension `5000`.
3. Dialplan starts `EAGI(voice_assistant_eagi.py)`.
4. EAGI receives caller audio through file descriptor `3`.
5. Python captures one utterance at a time.
6. Audio is converted to 24 kHz PCM and sent to OpenAI Realtime.
7. OpenAI returns:
   - user transcript,
   - assistant text,
   - assistant audio,
   - optional tool calls such as transfer, service request submission, or end call.
8. Python plays the assistant WAV back through Asterisk.
9. If a tool call was returned, Python performs the action.

High-level flow:

```text
Caller
  -> Asterisk PJSIP
  -> extensions.conf extension 5000
  -> voice_assistant_eagi.py
  -> OpenAIRealtimeClient
  -> OpenAI Realtime API
  -> voice_assistant_eagi.py action handling
  -> playback, transfer, webhook, Rainbow, email, wake-up API
```

## 3. Docker Services

Defined in `docker-compose.yml`:

- `asterisk`: runs Asterisk and the Python EAGI bot.
- `api`: runs FastAPI from `api/app/main.py`.
- `apache`: reverse proxy and frontend host.
- `frontend-build`: builds the guest web app.
- `freeradius`: RADIUS server for captive portal authentication.
- `certbot`: certificate helper.

The `asterisk` service mounts:

- `./agi` to `/var/lib/asterisk/agi-bin`
- `./logs` to `/var/log/asterisk/ai`
- Asterisk config files into `/etc/asterisk`

That means edits to `agi/*.py` are visible in the container, but you still need to restart/reload the runtime for a clean test.

## 4. Asterisk Dialplan

Main file: `asterisk/extensions.conf`

Important behavior:

- Extension `1900` receives inbound Rainbow trunk calls and routes them to `5000`.
- Extension `5000` answers and starts `voice_assistant_eagi.py`.
- It sets important channel variables:
  - `AI_CALLER_NAME`
  - `AI_CALLER_NUM`
  - `AI_SIP_FROM`
  - `EAGI_AUDIO_FORMAT=slin`

The Python script reads those variables using AGI commands.

## 5. Main Voicebot Entrypoint

File: `agi/voice_assistant_eagi.py`

This is the operational center of the voicebot.

Important responsibilities:

- Parse AGI environment.
- Answer the call.
- Enrich caller details from SIP headers.
- Extract caller name and room number.
- Play greeting.
- Capture caller audio from EAGI fd `3`.
- Filter weak/noisy/silent audio.
- Send valid audio turns to OpenAI Realtime.
- Process transcripts, assistant audio, and tool calls.
- Submit hotel service requests.
- Notify Rainbow bubbles.
- Send service request emails.
- Send transcript emails.
- Create wake-up calls through the FastAPI API.
- Transfer calls using SIP REFER through Asterisk `Transfer`.
- Detect hangups and stop safely.

### Key Functions

`run_call()`

Main async call loop. Most runtime behavior starts here.

`parse_agi_env()`

Reads initial AGI variables from stdin.

`enrich_session_caller_details(session)`

Reads `AI_CALLER_NAME`, `AI_CALLER_NUM`, and `AI_SIP_FROM`. It updates:

- `session.caller_name`
- `session.caller_id`
- `session.sip_from_header`
- `session.room_number`

`extract_room_number_from_caller_identity(...)`

Best-effort parser for room numbers from display names such as:

- `Samuel Yip - 1910 - EN`
- `1001 SG Operator`
- `Room 1910 - Samuel`

`build_greeting_text(session)`

Builds either a personalized greeting or a generic greeting.

`audio_input_should_be_ignored(...)`

Rejects weak audio before sending it to OpenAI. This prevents noisy or silent turns from causing bad transcripts.

`apply_known_room_number(session, payload)`

If OpenAI submits a service request without `room_number`, this fills the room number from caller identity when known.

`post_hotel_request(session, payload)`

Posts the confirmed service request to `HOTEL_REQUEST_WEBHOOK_URL`.

`notify_rainbow_service_request(session, payload)`

Sends service request details to Rainbow via `rainbow_service_request_notifier.js`.

`post_wakeup_call_request(session, payload)`

For confirmed `wake_up_call` service requests, posts to `WAKEUP_CALL_API_URL`, usually `http://host.docker.internal:8000/api/wakeup-call`.

`send_call_transcript_email(session)`

Sends the full transcript after the call ends.

## 6. OpenAI Realtime Client

File: `agi/openai_realtime_client.py`

This file defines:

- The assistant prompt.
- OpenAI tool schemas.
- Realtime websocket lifecycle.
- Prior call context replay.
- Function-call argument parsing.

### Assistant Prompt

Constant: `ASSISTANT_INSTRUCTIONS`

This prompt controls hospitality behavior. It tells the assistant to:

- collect missing service details,
- ask one question at a time,
- not ask for known room number,
- confirm important details,
- submit requests only after confirmation,
- transfer only when the guest explicitly asks,
- support multiple languages,
- end calls politely when the guest is done.

If changing behavior, start here before adding code.

### Tool Schemas

`TRANSFER_TOOL`

OpenAI calls this when the call should be transferred.

Expected destinations:

- `1920`: front desk, concierge, human support.
- `1921`: room service or in-room dining.
- Any guest room number for direct room transfer.

`SUBMIT_HOTEL_REQUEST_TOOL`

OpenAI calls this after collecting and confirming a service request.

Important fields:

- `category`
- `summary`
- `room_number`
- `preferred_time`
- `alarm_time`
- `followup_time`
- `frequency`
- `priority`
- `language`
- `confirmed_with_guest`

`END_CALL_TOOL`

OpenAI calls this only after the guest clearly says there is nothing else.

### Prior Call Context

Function: `build_prior_call_context(session)`

Every EAGI turn opens a fresh Realtime session. To preserve memory, this function sends compact prior context back to OpenAI.

It includes:

- preferred language,
- caller ID,
- caller name,
- known room number,
- latest service request,
- recent meaningful user/assistant dialogue.

It intentionally excludes noisy events like `audio_ignored` and `silence_prompt_played`.

## 7. Call Session And Language Detection

File: `agi/call_session.py`

`CallSession` stores all call-level state:

- `call_id`
- `caller_id`
- `caller_name`
- `room_number`
- `sip_from_header`
- `preferred_language`
- `history`
- transfer state
- timestamps

Every important event is written to:

```text
logs/calls/<call_id>.jsonl
```

Each line is one JSON object. This makes logs easy to inspect and test.

### Language Detection

Function: `detect_language_from_text(text, default)`

This is a deterministic guardrail around model language behavior.

It uses:

- language hints,
- script detection,
- confidence thresholds,
- low-information utterance filtering,
- English override phrases,
- CJK, kana, Hangul, Arabic, Tamil, Thai, Hindi checks.

The goal is not perfect language detection. The goal is to prevent obvious false switches and keep phone conversations stable.

### Deterministic Actions

`determine_transfer_action(...)`

Detects clear transfer phrases without waiting for model judgment.

`should_end_call_deterministic(...)`

Detects clear call-ending phrases.

These deterministic checks supplement OpenAI tool calls.

## 8. Audio Pipeline

File: `agi/audio_utils.py`

The voicebot is half-duplex and turn-based.

Flow:

1. Play assistant audio.
2. Drain EAGI fd `3` so playback echo does not become the next user input.
3. Listen for caller audio.
4. Stop after silence or max utterance length.
5. Reject too-short, too-quiet, or low-speech audio.
6. Convert to 24 kHz PCM for OpenAI.

Important environment variables:

```env
SILENCE_TIMEOUT_MS=900
MAX_UTTERANCE_SECONDS=15
NO_SPEECH_TIMEOUT_SECONDS=5
MIN_AUDIO_SECONDS=0.75
MIN_AUDIO_RMS_DBFS=-46
MIN_AUDIO_SPEECH_RATIO=0.08
AUDIO_SPEECH_THRESHOLD_DBFS=-38
EAGI_DRAIN_AFTER_PLAYBACK_MS=250
```

If transcription quality is poor, inspect the call log fields:

- `duration_seconds`
- `rms_dbfs`
- `speech_ratio`
- `audio_ignored`
- `transcript_ignored`

## 9. Service Request Flow

A normal service request goes through these steps:

1. User asks for a service.
2. OpenAI collects required details.
3. OpenAI repeats the request and asks for confirmation.
4. User confirms.
5. OpenAI calls `submit_hotel_request`.
6. Python verifies `confirmed_with_guest=true`.
7. Python fills `room_number` from caller identity if missing.
8. Python logs `service_request_action_detected`.
9. Python posts to `HOTEL_REQUEST_WEBHOOK_URL`.
10. Python queues Rainbow notification.
11. Python sends service request email.
12. Python plays "request submitted" prompt and asks if anything else is needed.

Key events:

- `service_request_action_detected`
- `service_request_confirmation_required`
- `service_request_submitted`
- `service_request_rainbow_queued`
- `service_request_rainbow_result`
- `service_request_email_result`

## 10. Wake-Up Call Flow

Wake-up calls use the normal service request flow plus an extra API integration.

Expected OpenAI tool payload:

```json
{
  "category": "wake_up_call",
  "summary": "Wake-up call for room 1910 at 6:30 AM tomorrow",
  "room_number": "1910",
  "preferred_time": "tomorrow at 6:30 AM",
  "alarm_time": "2026-05-11T06:30:00",
  "frequency": "Once",
  "priority": "normal",
  "language": "en",
  "confirmed_with_guest": true
}
```

Python then calls:

```text
WAKEUP_CALL_API_URL
```

Default:

```env
WAKEUP_CALL_API_URL=http://host.docker.internal:8000/api/wakeup-call
```

The FastAPI endpoint is in `api/app/main.py`:

```text
POST /api/wakeup-call
```

It expects:

- `room_number`
- `alarm_time`
- optional `followup_time`
- optional `frequency`

It then:

1. Logs into Rainbow Hospitality Gateway.
2. Finds the room by room number.
3. Gets the RHG room ID.
4. Calls `RainbowClient.create_wakeup_call(...)`.

Voicebot wake-up events:

- `service_request_submitted`
- `wakeup_call_result`
- `service_request_rainbow_queued`
- `service_request_email_result`

If wake-up creation fails, the normal front-desk notification still happens.

## 11. Transfer Flow

Transfers use Asterisk `Transfer`.

Default transfer target:

```env
TRANSFER_TARGET_TEMPLATE=sip:{extension}@313.apac1.sip.openrainbow.com
```

For example:

```text
sip:1920@313.apac1.sip.openrainbow.com
```

Transfer types:

- `human`: front desk or concierge, extension `1920`.
- `room_service`: in-room dining team, extension `1921`.
- `room`: direct guest room number.

Current behavior accepts OpenAI transfer tool calls directly after normalization. There is no local suppression gate.

Key events:

- `transfer_action_detected`
- `transfer_requested`
- `transfer_result`
- `transfer_unavailable`
- `channel_status_after_transfer_failure`
- `hangup`

## 12. Rainbow Bubble Notifications

File: `agi/rainbow_service_request_notifier.js`

This script receives JSON on stdin from Python and sends a formatted message to a Rainbow bubble.

Destination is chosen by `rainbow_service_request_destination(payload)`:

- `room_service` and `housekeeping` go to `RAINBOW_ROOM_SERVICE_BUBBLE_JID` by default.
- Other categories go to `RAINBOW_FRONT_DESK_BUBBLE_JID`.

Important config:

```env
RAINBOW_NODE_NOTIFICATIONS_ENABLED=true
RAINBOW_FRONT_DESK_BUBBLE_JID=
RAINBOW_ROOM_SERVICE_BUBBLE_JID=
RAINBOW_ROOM_SERVICE_CATEGORIES=room_service,housekeeping
RAINBOW_NODE_ASYNC=true
```

`RAINBOW_NODE_ASYNC=true` is important for live calls. It prevents Rainbow notification latency from blocking the caller.

## 13. Email Behavior

Two email types exist:

1. Full transcript email after call end.
2. Service request email after a request is submitted.

Config:

```env
EMAIL_TRANSCRIPT_ENABLED=true
TRANSCRIPT_EMAIL_TO=
TRANSCRIPT_EMAIL_FROM=
EMAIL_FROM_NAME=Hotel Voicebot
SERVICE_REQUEST_EMAIL_ENABLED=true
SERVICE_REQUEST_EMAIL_TO=
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_STARTTLS=true
```

If `SERVICE_REQUEST_EMAIL_TO` is blank, service request email falls back to `TRANSCRIPT_EMAIL_TO`.

## 14. FastAPI Backend

File: `api/app/main.py`

Important endpoints:

- `GET /`
- `POST /radius/auth`
- `POST /api/guest/auth`
- `GET /api/rainbow/config`
- `POST /api/flows/new-request`
- `POST /api/wakeup-call`

Important helper functions:

- `build_pms_client()`: builds Rainbow Hospitality client.
- `fetch_rooms()`: gets rooms from RHG.
- `find_room_by_number(room_number)`: finds a room.
- `validate_guest(room_number, last_name)`: validates guest login.
- `wakeup_call_proxy(payload)`: creates RHG wake-up call.

The API needs `api/app/.env` with Rainbow Hospitality credentials.

## 15. FreeRADIUS

The `freeradius` service supports captive portal auth.

High-level path:

1. RADIUS receives auth request.
2. FreeRADIUS REST module calls FastAPI `/radius/auth`.
3. FastAPI validates room number and guest last name against RHG room data.
4. Access is granted or rejected.

Use FreeRADIUS debug mode when troubleshooting RADIUS packets:

```bash
docker compose run --rm --service-ports freeradius freeradius -X
```

## 16. Frontend

The frontend is under `frontend/`.

It is a guest-facing browser app and uses Rainbow Web SDK.

Build with:

```bash
docker compose --profile build run --rm frontend-build
```

The built files are copied to `frontend/dist` and served through Apache.

## 17. Configuration Files

Do not commit real `.env` files.

Important templates:

- `agi/.env.example`
- `api/app/env.sample`

Important runtime configs:

- `asterisk/extensions.conf`
- `asterisk/pjsip.conf.template`
- `asterisk/rtp.conf`
- `asterisk/modules.conf`
- `apache/apache.conf`
- `docker-compose.yml`

## 18. Logs

Voicebot structured call logs:

```text
logs/calls/<call_id>.jsonl
```

Each line is JSON. Common event types:

- `call_started`
- `greeting_played`
- `audio_captured`
- `audio_ignored`
- `transcript_ignored`
- `language_change`
- `user`
- `assistant`
- `assistant_audio_played`
- `service_request_action_detected`
- `service_request_submitted`
- `wakeup_call_result`
- `service_request_rainbow_queued`
- `service_request_rainbow_result`
- `service_request_email_result`
- `transfer_action_detected`
- `transfer_result`
- `end_call_action_detected`
- `call_closing`
- `hangup`
- `transcript_email_result`

Container logs:

```bash
docker logs -f aivoicebot-asterisk
docker compose logs -f api
docker compose logs -f apache
docker compose logs -f freeradius
```

## 19. Local Development Workflow

Set up Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r agi\requirements.txt
```

Run tests:

```powershell
python -m pytest -q
```

Compile-check Python files:

```powershell
python -m py_compile agi\voice_assistant_eagi.py agi\openai_realtime_client.py agi\call_session.py agi\audio_utils.py
```

Restart containers after runtime changes:

```powershell
docker compose restart asterisk
docker compose restart api
```

## 20. How To Add A New Service Request Type

Example: add laundry pickup.

1. Update `ASSISTANT_INSTRUCTIONS` in `agi/openai_realtime_client.py`.
2. Add the new category to `SUBMIT_HOTEL_REQUEST_TOOL`.
3. Add required fields to the prompt instructions.
4. If it needs an external API, add a function in `voice_assistant_eagi.py`.
5. Call that function in the `result.service_request_action` block.
6. Add tests in `tests/test_transfer_decision.py` or a new test file.
7. Add `.env.example` settings if configuration is needed.
8. Update README and this guide.

## 21. How To Change Prompt Behavior Safely

Prompt changes are risky because they affect live call flow.

Before changing code, ask:

- Is this behavior purely conversational? Change the prompt.
- Is it a deterministic safety rule? Change `call_session.py`.
- Is it an integration action? Change `voice_assistant_eagi.py`.
- Is it OpenAI tool schema or parsing? Change `openai_realtime_client.py`.

Always add tests that check:

- the prompt contains the key instruction,
- the tool parser accepts expected payloads,
- the deterministic guard handles the phrase,
- the final action payload has the right fields.

## 22. How To Debug A Bad Call

1. Open the latest `logs/calls/*.jsonl`.
2. Confirm `call_started` has correct caller ID, caller name, and room number.
3. Check `greeting_played`.
4. Check `audio_captured` quality:
   - `rms_dbfs`
   - `speech_ratio`
   - `duration_seconds`
5. Check `user` transcript.
6. Check `language_change`.
7. Check `assistant` text.
8. Check tool events:
   - transfer,
   - service request,
   - wake-up result,
   - end call.
9. Check external integration events:
   - webhook result,
   - Rainbow result,
   - email result.

If the transcript is wrong, inspect audio quality first. If audio quality is good but language is wrong, check `detect_language_from_text`. If OpenAI asks the wrong question, check prior call context and prompt instructions.

## 23. Common Change Locations

Use this map to find the right file quickly:

| Task | File |
| --- | --- |
| Change assistant behavior | `agi/openai_realtime_client.py` |
| Add OpenAI tool field | `agi/openai_realtime_client.py` |
| Parse model tool arguments | `agi/openai_realtime_client.py` |
| Add service integration | `agi/voice_assistant_eagi.py` |
| Change transfer target | `agi/.env`, `agi/voice_assistant_eagi.py` |
| Change language switching | `agi/call_session.py` |
| Change silence handling | `agi/voice_assistant_eagi.py`, `agi/audio_utils.py` |
| Change audio thresholds | `agi/.env` |
| Change Rainbow message behavior | `agi/rainbow_service_request_notifier.js` |
| Change RHG wake-up API | `api/app/main.py` |
| Change SIP routing | `asterisk/extensions.conf` |
| Change NAT/RTP | `asterisk/pjsip.conf.template`, `asterisk/rtp.conf` |
| Change captive portal auth | `freeradius/`, `api/app/main.py` |

## 24. Test Ownership

Current test files:

- `tests/test_audio_conversion.py`: audio conversion and WAV behavior.
- `tests/test_agi_channel_state.py`: AGI response parsing, caller name, room number extraction.
- `tests/test_language_detection.py`: language detection guardrails.
- `tests/test_prompt_instructions.py`: prompt and prior context requirements.
- `tests/test_transfer_decision.py`: transfer, service request, Rainbow, wake-up API helpers.
- `tests/test_transcript_email.py`: transcript and service request email behavior.

When adding a feature, add or update tests in the closest file.

## 25. Coding Rules For This Repo

- Keep runtime behavior explicit and logged.
- Prefer small helper functions with tests.
- Do not block the live call on slow external integrations unless required.
- Never commit real secrets.
- Preserve the structured JSONL call log style.
- Keep prompts concise but specific.
- Avoid broad refactors while debugging production call behavior.
- After every behavior change, run `python -m pytest -q`.

## 26. Safe First Tasks For A Junior Developer

Good starter tasks:

- Add a new phrase to language hints.
- Add a new test case for transfer detection.
- Improve README troubleshooting notes.
- Add a new service request email field.
- Add logging for an existing action.
- Add a new call-log event for a clearly defined branch.

Avoid as first tasks:

- Rewriting the EAGI audio loop.
- Changing the transfer mechanism.
- Changing the Realtime websocket lifecycle.
- Changing SIP/TLS trunk settings.
- Changing the wake-up API without tests.

