from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from call_session import (
    SUPPORTED_LANGUAGES,
    CallSession,
    detect_language_from_text,
    determine_transfer_action,
    language_response_instruction,
)


LOGGER = logging.getLogger(__name__)


REALTIME_URL = "wss://api.openai.com/v1/realtime"


ASSISTANT_INSTRUCTIONS = """You are a warm, professional, multilingual Hospitality Concierge voice assistant for a premium hotel. Speak like a calm, polished concierge: friendly, efficient, discreet, and attentive.

Primary mission:
- Understand the guest's request, collect the minimum missing context needed to help, confirm important details, then either assist directly or route the guest to the right hotel team.
- Do not give generic replies such as "How can I help?" after the guest has already stated a request. Move the conversation forward by asking the next useful question.
- The system already played the initial greeting at call start. Do not reintroduce yourself as Nova again during the call unless the guest asks who you are.
- Do not ask for all details at once. Ask one focused question at a time, prioritizing the detail that blocks progress.
- Keep each spoken response short enough for phone playback: usually 1-3 sentences.

Conversation method:
1. Identify the service category: front desk/concierge, wake-up call, housekeeping, room service, dining reservation, transportation, local recommendation, spa, event support, emergency/safety, complaint, lost item, or general information.
2. Extract any details already provided by the guest. Do not ask for details already known.
3. If Prior call context includes a Known details room_number, use that room number for hotel service requests. Do not ask for the room number again unless the guest says it is wrong, gives a different room, or the known room number is missing.
4. Ask for the next missing required detail for that category.
5. For critical details, repeat them back and ask for confirmation.
6. For routine service requests, collect the required details, repeat the complete request back to the guest, and ask for explicit confirmation before submitting. Only call submit_hotel_request after the guest clearly confirms with words like yes, correct, confirmed, that's right, okay, please proceed, or equivalent in the guest's language.
7. Transfer only when the guest explicitly asks to speak to, call, connect to, or be transferred to a person or team, or when there is an emergency/safety issue. Do not call transfer_to_extension merely because the guest asks for food, housekeeping, room cleaning, a wake-up call, transportation, a recommendation, or another routine service request.
8. When the guest clearly says there are no more requests, thanks you and says goodbye, says "that's all", "nothing else", "no more", "no thank you", "bye", or similar, give a brief courteous closing in the guest's current language, then call the end_call tool. Do not ask another follow-up question after the guest has closed the conversation.

Details to collect by request type:
- Wake-up call: room number, date, time, AM/PM or 24-hour confirmation, one-time or repeated wake-up call, guest name if offered, preferred language if relevant. If the room number is already in Known details, use it and do not ask again. Convert the requested wake-up time to local hotel ISO format in alarm_time, for example 2026-05-10T06:30:00. Set followup_time only if a follow-up call is requested. Use frequency "Once" unless the guest asks for repeated wake-up calls. Repeat the full details and ask the guest to confirm. Only after confirmation, call submit_hotel_request with category wake_up_call and confirmed_with_guest true.
- Housekeeping: room number, item/service needed, preferred timing, urgency, access/privacy preference if relevant. For room cleaning, ask for the room number first if missing, then ask preferred timing or whether housekeeping may enter if needed. Repeat the full details and ask the guest to confirm. Only after confirmation, call submit_hotel_request with category housekeeping and confirmed_with_guest true.
- Maintenance: room number, issue, severity, safety risk, whether someone may enter the room, preferred timing. Transfer urgent or safety-related issues to 1920.
- Transportation: pickup/drop-off location, date/time, number of passengers, luggage, vehicle preference, child seat/accessibility needs, contact/room number.
- Dining recommendation or reservation: cuisine, date/time, number of guests, budget, dietary restrictions/allergies, occasion, location preference. Confirm reservation details if booking is requested.
- Spa booking: treatment type, date/time preference, number of guests, therapist preference if relevant, health considerations.
- Local attractions or itinerary: interests, available time, group size, mobility constraints, budget, weather sensitivity, starting location.
- Lost item: item description, last known location/time, guest name, room number, contact number.
- Complaint: acknowledge, apologize briefly, collect room number and issue, ask desired resolution if unclear, escalate to 1920 when appropriate.
- Emergency, medical, security, fire, threat, or distressed guest: keep the guest calm, ask location/room number if not known, and transfer immediately to 1920. Do not delay with extra questions.

Human agent transfer:
- If the guest asks to speak to, call, contact, connect to, or transfer to a person, human, operator, receptionist, front desk, staff member, concierge team, manager, or agent, acknowledge politely and initiate transfer.
- Treat phrases such as "talk to agent," "speak to human," "operator please," "connect me to reception," "front desk," "I want a person," "human support," "manager," "live agent," and similar requests as human-agent transfer requests.
- Do not use this route for routine service-intake requests. For example, "I want to order food", "I need room cleaning", or "book me a taxi" should be handled by collecting details and confirming the request, not by transferring to 1920.
- Say: "Of course. I'll connect you to our concierge team now. Please hold for a moment."
- Then call the transfer_to_extension tool with extension 1920.
- Do not continue troubleshooting or asking unnecessary questions once the guest clearly requests a human.

Direct room transfer:
- If the guest explicitly asks to call, connect to, transfer to, or reach a guest room, use the room number they gave as the transfer destination.
- Treat phrases such as "connect me to room 1208", "transfer me to room 1002", "call room 2301", "put me through to 808", and similar requests as direct room transfer requests.
- If the room number is clear, say: "Of course. I'll connect you to the room now. Please hold for a moment."
- Then call the transfer_to_extension tool with action transfer, extension set to the room number as digits, and transfer_type set to room.
- If the room number is missing or unclear, ask one focused question for the room number.
- Do not create a hotel service request for direct room transfer requests.

Room service and in-room dining:
- Treat "room service," "in-room dining," "order food to my room," "send food upstairs," "breakfast in the room," and "dining delivery" as service requests to collect and submit, not automatic transfers.
- If the guest says "I want to order food" or similar, ask what they would like to order and collect the room-service details. Do not transfer unless they explicitly ask to speak to or connect to room-service staff.
- Collect room number, requested items or general request, preferred delivery time, number of guests if relevant, allergies/dietary/religious requirements, and any special notes. Ask one question at a time.
- When the details are sufficient, repeat the room-service request back to the guest and ask for confirmation. Only after the guest confirms, call submit_hotel_request with category room_service and confirmed_with_guest true.
- Transfer to 1921 only if the guest explicitly asks to speak to, call, connect to, or transfer to room service or in-room dining staff, or if the request is an order status/change/complaint that requires live staff. In that case, call the transfer_to_extension tool with extension 1921.

Multilingual support:
- Respond in the same language the guest uses.
- If the guest switches language, switch immediately in the next response.
- If the language is unclear, ask politely which language they prefer.
- Support English, Mandarin Chinese, Cantonese, Malay, Tamil, Japanese, Korean, Thai, Vietnamese, Indonesian, French, Spanish, German, Italian, Arabic, Hindi, and other major guest languages where possible.
- Use simple, clear language for non-native speakers.
- Avoid idioms, slang, or culturally confusing expressions unless the guest uses them first.
- When translating, preserve meaning, tone, and hospitality style rather than translating word-for-word.

Confirmation rules:
- Always confirm dates, times, room numbers, names, phone numbers, destination addresses, number of guests, prices, allergies, dietary needs, accessibility needs, transport instructions, emergency locations, and wake-up call details.
- Never call submit_hotel_request with confirmed_with_guest false. If the request is not confirmed yet, ask the guest to confirm instead of calling the tool.
- If a detail sounds ambiguous, ask a single clarifying question.
- If you cannot complete an action, explain briefly and offer the next best step or transfer.

Call ending:
- If the guest declines further help or closes the conversation, say a short premium-hospitality goodbye such as "You're very welcome. Thank you for calling, and have a pleasant day."
- Then call the end_call tool with action end_call and a short reason.
- Only end the call when the guest clearly has no further request, or after a successful transfer/request flow when the guest confirms nothing else is needed.

Style examples:
- "Of course, I'd be happy to help. May I have your room number, please?"
- "Certainly. For the wake-up call, what time would you like us to call?"
- "Thank you. That's room 1208, tomorrow at 6:30 AM, one wake-up call. May I confirm that is correct?"
- "That sounds lovely. May I check your preferred cuisine and the number of guests?"
- "Of course. I'll connect you to our in-room dining team now. Please hold for a moment."
- "Of course. I can arrange room cleaning for you. May I have your room number, please?"
- "Thank you. That's room 1208 for room cleaning this afternoon, and housekeeping may enter if you are out. May I confirm that is correct?"

Always prioritize guest comfort, clarity, safety, privacy, cultural sensitivity, and a premium hospitality experience."""


TRANSFER_TOOL = {
    "type": "function",
    "name": "transfer_to_extension",
    "description": "Request transfer of the live call only when the guest explicitly asks to speak, call, connect, transfer, or be put through to a hotel team, human, or guest room. Do not use for routine service requests such as ordering food or room cleaning.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["transfer"]},
            "extension": {
                "type": "string",
                "pattern": "^[0-9]{2,8}$",
                "description": "Transfer destination. Use 1920 for concierge/front desk/human support, 1921 for room service/in-room dining, or the guest room number for direct room transfer.",
            },
            "transfer_type": {
                "type": "string",
                "enum": ["human", "room_service", "room"],
                "description": "Use room only when transferring directly to a guest room number.",
            },
            "reason": {"type": "string", "description": "Short reason for transfer."},
        },
        "required": ["action", "extension", "reason"],
        "additionalProperties": False,
    },
}


SUBMIT_HOTEL_REQUEST_TOOL = {
    "type": "function",
    "name": "submit_hotel_request",
    "description": "Submit a completed non-emergency hotel guest service request to the hotel operations system.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "wake_up_call",
                    "housekeeping",
                    "maintenance",
                    "transportation",
                    "dining_reservation",
                    "room_service",
                    "spa",
                    "local_recommendation",
                    "lost_item",
                    "complaint",
                    "general_guest_request",
                ],
            },
            "summary": {"type": "string", "description": "Concise summary of what the guest needs."},
            "room_number": {"type": "string", "description": "Guest room number if known."},
            "guest_name": {"type": "string", "description": "Guest name if known."},
            "preferred_time": {"type": "string", "description": "Requested time/date or timing preference if relevant."},
            "alarm_time": {"type": "string", "description": "For wake_up_call only: local hotel ISO datetime, for example 2026-05-10T06:30:00."},
            "followup_time": {"type": "string", "description": "For wake_up_call only: optional local hotel ISO datetime for a follow-up wake-up call."},
            "frequency": {"type": "string", "enum": ["Once", "Daily", "Weekly"], "description": "For wake_up_call only: use Once unless the guest requests a repeated wake-up call."},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            "access_permission": {"type": "string", "description": "Whether staff may enter the room, if relevant."},
            "language": {"type": "string", "description": "Guest's current preferred language code."},
            "notes": {"type": "string", "description": "Dietary needs, allergies, accessibility needs, contact details, or other important context."},
            "confirmed_with_guest": {
                "type": "boolean",
                "description": "Must be true. Only call this tool after repeating key details and receiving explicit guest confirmation.",
            },
        },
        "required": ["category", "summary", "priority", "language", "confirmed_with_guest"],
        "additionalProperties": False,
    },
}


END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": "End the live call after the guest clearly indicates there are no more requests.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["end_call"]},
            "reason": {"type": "string", "description": "Short reason for ending the call."},
        },
        "required": ["action", "reason"],
        "additionalProperties": False,
    },
}


def build_prior_call_context(session: CallSession, max_dialogue_events: int = 12) -> str:
    """Build compact memory for the fresh Realtime session used on each EAGI turn."""
    dialogue: list[str] = []
    known: dict[str, str] = {}
    pending: dict[str, Any] = {}

    for event in session.history:
        event_type = event.get("type")
        text = str(event.get("text") or "").strip()

        if event_type == "user" and text:
            dialogue.append(f"Guest: {text}")
            room_match = re.search(r"\b(?:room|房间|房間|房号|房號)?\s*(\d{3,6})\b", text, re.IGNORECASE)
            if room_match:
                known["room_number"] = room_match.group(1)
            chinese_room_digits = re.search(r"(?:房间|房間|房号|房號).{0,10}?(\d{3,6})", text)
            if chinese_room_digits:
                known["room_number"] = chinese_room_digits.group(1)
            chinese_room_match = re.search(r"(?:房间|房間|房号|房號).{0,8}([一二三四五六七八九零〇两]{3,6})", text)
            if chinese_room_match:
                known["room_number_spoken"] = chinese_room_match.group(1)
        elif event_type == "assistant" and text:
            dialogue.append(f"Assistant: {text}")
        elif event_type == "service_request_action_detected":
            pending = {
                "category": event.get("category"),
                "summary": event.get("summary"),
                "room_number": event.get("room_number"),
                "preferred_time": event.get("preferred_time"),
                "confirmed_with_guest": event.get("confirmed_with_guest"),
            }
            if event.get("room_number"):
                known["room_number"] = str(event.get("room_number"))
        elif event_type == "service_request_confirmation_required":
            request = event.get("request", {})
            pending = {
                "status": "awaiting_guest_confirmation",
                "category": request.get("category"),
                "summary": request.get("summary"),
                "room_number": request.get("room_number"),
                "preferred_time": request.get("preferred_time"),
                "confirmed_with_guest": False,
            }
            if request.get("room_number"):
                known["room_number"] = str(request.get("room_number"))
        elif event_type == "service_request_submitted":
            request = event.get("payload", {}).get("request", {})
            if request:
                pending = {
                    "submitted": "true",
                    "category": request.get("category"),
                    "summary": request.get("summary"),
                    "room_number": request.get("room_number"),
                    "preferred_time": request.get("preferred_time"),
                }
                if request.get("room_number"):
                    known["room_number"] = str(request.get("room_number"))

    lines = [
        f"Current preferred language: {session.preferred_language}",
        f"Caller ID: {session.caller_id}",
    ]
    if session.caller_name:
        lines.append(f"Caller name: {session.caller_name}")
    if session.room_number:
        known["room_number"] = session.room_number
        lines.append(f"Caller room number from caller identity: {session.room_number}")
    if known:
        lines.append("Known details: " + json.dumps(known, ensure_ascii=False))
    if pending:
        lines.append("Current or latest service request: " + json.dumps(pending, ensure_ascii=False))
    if dialogue:
        lines.append("Recent meaningful dialogue:")
        lines.extend(dialogue[-max_dialogue_events:])
    lines.append("Use the known details above. Do not ask again for details already present unless the caller changes them.")
    return "\n".join(lines)


@dataclass
class RealtimeTurnResult:
    transcript: str = ""
    response_text: str = ""
    response_audio_pcm24k: bytes = b""
    language: str | None = None
    transfer_action: dict[str, Any] | None = None
    service_request_action: dict[str, Any] | None = None
    end_call_action: dict[str, Any] | None = None
    error: str | None = None


class OpenAIRealtimeClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        transfer_extension: str = "1920",
        room_service_extension: str | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
        self.transfer_extension = transfer_extension
        self.room_service_extension = room_service_extension or os.getenv("ROOM_SERVICE_TRANSFER_EXTENSION", "1921")
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

    async def process_turn(self, session: CallSession, pcm24k: bytes) -> RealtimeTurnResult:
        deterministic_transfer = determine_transfer_action(
            " ".join(e.get("text", "") for e in session.history[-2:]),
            session.failed_intent_count,
            human_extension=self.transfer_extension,
            room_service_extension=self.room_service_extension,
        )
        result = await asyncio.wait_for(self._process_turn_ws(session, pcm24k), timeout=self.timeout_seconds)
        language, confidence = detect_language_from_text(result.transcript, session.preferred_language)
        result.language = language
        session.update_language(language, confidence)

        if deterministic_transfer and result.transfer_action is None:
            result.transfer_action = deterministic_transfer

        return result

    async def _process_turn_ws(self, session: CallSession, pcm24k: bytes) -> RealtimeTurnResult:
        url = f"{REALTIME_URL}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        result = RealtimeTurnResult()
        audio_b64 = base64.b64encode(pcm24k).decode("ascii")
        import websockets

        async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
            await self._send_session_update(ws, session)
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64}))
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            early_events = await self._collect_transcription_before_response(ws, session, result)
            await ws.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "output_modalities": ["audio"],
                            "instructions": self._turn_instructions(session),
                        },
                    }
                )
            )

            function_call_args: dict[str, str] = {}
            for event in early_events:
                self._handle_event(event, result, function_call_args)
            while True:
                event = json.loads(await ws.recv())
                event_type = self._handle_event(event, result, function_call_args)
                if event_type == "response.done":
                    self._extract_tool_call(event, result)
                    for raw_args in function_call_args.values():
                        parsed = self._parse_transfer_args(raw_args)
                        if parsed:
                            result.transfer_action = parsed
                        parsed_request = self._parse_service_request_args(raw_args)
                        if parsed_request:
                            result.service_request_action = parsed_request
                        parsed_end_call = self._parse_end_call_args(raw_args)
                        if parsed_end_call:
                            result.end_call_action = parsed_end_call
                    break

        return result

    async def _collect_transcription_before_response(
        self,
        ws: Any,
        session: CallSession,
        result: RealtimeTurnResult,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        if timeout_seconds is None:
            timeout_seconds = float(os.getenv("OPENAI_TRANSCRIPTION_PREFLIGHT_TIMEOUT_SECONDS", "2.0"))
        buffered_events: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            event_type = event.get("type", "")
            buffered_events.append(event)
            if event_type == "conversation.item.input_audio_transcription.completed":
                result.transcript = event.get("transcript", "")
                language, confidence = detect_language_from_text(result.transcript, session.preferred_language)
                session.update_language(language, confidence, source="input_transcription")
                break
            if event_type == "error":
                raise RuntimeError(f"OpenAI Realtime error: {event.get('error', event)}")
        return buffered_events

    def _turn_instructions(self, session: CallSession) -> str:
        language_code = session.preferred_language
        language_name = SUPPORTED_LANGUAGES.get(language_code, language_code)
        timezone_name = os.getenv("HOTEL_TIMEZONE", "Asia/Singapore")
        try:
            now = datetime.now(ZoneInfo(timezone_name))
        except Exception:
            timezone_name = "local"
            now = datetime.now().astimezone()
        current_time_hint = f"Hotel local datetime is {now.strftime('%Y-%m-%dT%H:%M:%S')} ({timezone_name}). "
        room_hint = ""
        if session.room_number:
            room_hint = (
                f" Known caller room number is {session.room_number}; use it for hotel service requests "
                "and do not ask for the room number again unless the guest corrects it."
            )
        return (
            f"Detected caller language for this turn: {language_name} ({language_code}). "
            f"{language_response_instruction(language_code)} "
            f"{current_time_hint}"
            f"{room_hint} "
            "This turn's detected language overrides prior conversation language. "
            "If the guest explicitly asks to use another language, switch to that requested language immediately. "
            "Do not reintroduce yourself; the call greeting has already been played. "
            "Keep the response concise for a phone call."
        )

    def _handle_event(
        self,
        event: dict[str, Any],
        result: RealtimeTurnResult,
        function_call_args: dict[str, str],
    ) -> str:
        event_type = event.get("type", "")
        LOGGER.debug("Realtime event: %s", event_type)

        if event_type == "conversation.item.input_audio_transcription.completed":
            result.transcript = event.get("transcript", "")
        elif event_type in {
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
            "response.output_text.delta",
            "response.text.delta",
        }:
            result.response_text += event.get("delta", "")
        elif event_type in {"response.audio.delta", "response.output_audio.delta"}:
            result.response_audio_pcm24k += base64.b64decode(event.get("delta", ""))
        elif event_type in {"response.function_call_arguments.delta", "response.tool_call_arguments.delta"}:
            call_id = event.get("call_id") or event.get("item_id") or "default"
            function_call_args[call_id] = function_call_args.get(call_id, "") + event.get("delta", "")
        elif event_type in {"response.output_item.done", "conversation.item.created"}:
            self._extract_tool_call(event, result)
        elif event_type == "response.done":
            response = event.get("response", {})
            status = response.get("status")
            status_details = response.get("status_details")
            if status and status != "completed":
                result.error = json.dumps({"status": status, "status_details": status_details}, ensure_ascii=False)
        elif event_type == "error":
            raise RuntimeError(f"OpenAI Realtime error: {event.get('error', event)}")
        return event_type

    async def _send_session_update(self, ws: Any, session: CallSession) -> None:
        noise_reduction = os.getenv("OPENAI_REALTIME_NOISE_REDUCTION", "near_field").strip().lower()
        noise_reduction_config = None
        if noise_reduction not in {"", "none", "null", "off", "false", "disabled"}:
            if noise_reduction not in {"near_field", "far_field"}:
                noise_reduction = "near_field"
            noise_reduction_config = {"type": noise_reduction}
        payload = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.model,
                "instructions": ASSISTANT_INSTRUCTIONS,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "noise_reduction": noise_reduction_config,
                        "transcription": {"model": "gpt-4o-transcribe"},
                        "turn_detection": None,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": os.getenv("OPENAI_REALTIME_VOICE", "marin"),
                    },
                },
                "tools": [TRANSFER_TOOL, SUBMIT_HOTEL_REQUEST_TOOL, END_CALL_TOOL],
                "tool_choice": "auto",
            },
        }
        await ws.send(json.dumps(payload))

        if session.history:
            context = build_prior_call_context(session)
            await ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "system",
                            "content": [{"type": "input_text", "text": f"Prior call context:\n{context}"}],
                        },
                    }
                )
            )

    def _extract_tool_call(self, event: dict[str, Any], result: RealtimeTurnResult) -> None:
        raw = json.dumps(event)
        if "transfer_to_extension" not in raw and "submit_hotel_request" not in raw and "end_call" not in raw:
            return
        for key in ("arguments", "args"):
            if key in event:
                parsed = self._parse_transfer_args(event[key])
                if parsed:
                    result.transfer_action = parsed
                parsed_request = self._parse_service_request_args(event[key])
                if parsed_request:
                    result.service_request_action = parsed_request
                parsed_end_call = self._parse_end_call_args(event[key])
                if parsed_end_call:
                    result.end_call_action = parsed_end_call
        item = event.get("item") or event.get("response") or {}
        for output in item.get("output", []) if isinstance(item, dict) else []:
            name = output.get("name", "")
            if name == "transfer_to_extension":
                parsed = self._parse_transfer_args(output.get("arguments", ""))
                if parsed:
                    result.transfer_action = parsed
            elif name == "submit_hotel_request":
                parsed = self._parse_service_request_args(output.get("arguments", ""))
                if parsed:
                    result.service_request_action = parsed
            elif name == "end_call":
                parsed = self._parse_end_call_args(output.get("arguments", ""))
                if parsed:
                    result.end_call_action = parsed
        if isinstance(item, dict) and item.get("name") == "submit_hotel_request":
            parsed = self._parse_service_request_args(item.get("arguments", ""))
            if parsed:
                result.service_request_action = parsed
        if isinstance(item, dict) and item.get("name") == "transfer_to_extension":
            parsed = self._parse_transfer_args(item.get("arguments", ""))
            if parsed:
                result.transfer_action = parsed
        if isinstance(item, dict) and item.get("name") == "end_call":
            parsed = self._parse_end_call_args(item.get("arguments", ""))
            if parsed:
                result.end_call_action = parsed

    def _parse_transfer_args(self, raw_args: Any) -> dict[str, Any] | None:
        if not raw_args:
            return None
        if isinstance(raw_args, dict):
            data = raw_args
        else:
            try:
                data = json.loads(raw_args)
            except (TypeError, json.JSONDecodeError):
                return None
        if data.get("action") == "transfer":
            data.setdefault("extension", self.transfer_extension)
            data.setdefault("reason", "model requested transfer")
            return data
        return None

    def _parse_service_request_args(self, raw_args: Any) -> dict[str, Any] | None:
        if not raw_args:
            return None
        if isinstance(raw_args, dict):
            data = raw_args
        else:
            try:
                data = json.loads(raw_args)
            except (TypeError, json.JSONDecodeError):
                return None
        if data.get("category") and data.get("summary"):
            data.setdefault("priority", "normal")
            data.setdefault("confirmed_with_guest", False)
            return data
        return None

    def _parse_end_call_args(self, raw_args: Any) -> dict[str, Any] | None:
        if not raw_args:
            return None
        if isinstance(raw_args, dict):
            data = raw_args
        else:
            try:
                data = json.loads(raw_args)
            except (TypeError, json.JSONDecodeError):
                return None
        if data.get("action") == "end_call":
            data.setdefault("reason", "guest indicated there are no more requests")
            return data
        return None
