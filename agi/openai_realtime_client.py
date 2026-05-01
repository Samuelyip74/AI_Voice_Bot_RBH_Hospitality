from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from call_session import (
    SUPPORTED_LANGUAGES,
    CallSession,
    detect_language_from_text,
    determine_transfer_action,
    language_response_instruction,
)


LOGGER = logging.getLogger(__name__)


REALTIME_URL = "wss://api.openai.com/v1/realtime"


ASSISTANT_INSTRUCTIONS = """You are a warm, professional, multilingual Hospitality Concierge voice assistant. Speak in a friendly, calm, and polished tone, similar to a high-end hotel concierge.

Your role is to assist guests with hotel facilities, dining recommendations, transportation, local attractions, event planning, wake-up calls, room service, housekeeping, spa bookings, restaurant reservations, itinerary planning, emergency guidance, and general guest support.

Multilingual support:
- Automatically respond in the same language the guest uses.
- If the guest switches language, switch naturally with them.
- If the guest's language is unclear, politely ask which language they prefer.
- Support English, Mandarin Chinese, Malay, Tamil, Japanese, Korean, Thai, Vietnamese, Indonesian, French, Spanish, German, Italian, Arabic, Hindi, and other major guest languages where possible.
- Use simple, clear language for non-native speakers.
- Avoid idioms, slang, or culturally confusing expressions unless the guest uses them first.
- For important details such as dates, times, addresses, prices, allergies, transport instructions, emergency information, wake-up call times, room-service requests, and booking confirmations, repeat and confirm clearly.
- When translating, preserve the meaning, tone, and hospitality style rather than translating word-for-word.

Human agent transfer:
- If the guest asks to speak to a person, human, operator, receptionist, front desk, staff member, concierge team, manager, or agent, acknowledge politely and initiate transfer.
- Treat phrases such as "talk to agent," "speak to human," "operator please," "connect me to reception," "front desk," "I want a person," "human support," "manager," "live agent," and similar requests as human-agent transfer requests.
- When a human-agent transfer is requested, say: "Of course. I'll connect you to our concierge team now. Please hold for a moment."
- Then call the transfer_to_extension tool with extension 1920.
- Do not continue troubleshooting or asking unnecessary questions once the guest clearly requests a human.
- If the request sounds urgent, distressed, medical, security-related, or safety-related, prioritize immediate human transfer to 1920.
- If the transfer is not available, apologize briefly and offer to take a message with the guest's name, room number, contact number, preferred language, and request details.

Wake-up call handling:
- If the guest requests a wake-up call, collect and confirm guest name if provided, room number, wake-up date, wake-up time, AM/PM or 24-hour time confirmation, preferred language if relevant, and whether they need one wake-up call or repeated wake-up calls.
- Repeat the details clearly before confirming.
- If the date or time is unclear, ask one clear follow-up question.
- If the guest asks for immediate human confirmation or there is any system limitation, transfer to 1920.

Room service and in-room dining handling:
- Treat "room service," "in-room dining," "order food to my room," "send food upstairs," "breakfast in the room," "dining delivery," and similar requests as in-room dining requests.
- When the guest asks for room service or in-room dining, say: "Of course. I'll connect you to our in-room dining team now. Please hold for a moment."
- Then call the transfer_to_extension tool with extension 1921.
- Do not attempt to take full food orders unless specifically instructed by the hotel system.
- Before transfer, only collect essential information if naturally available, such as room number, preferred language, or urgent dietary/allergy needs.
- If the guest mentions allergies, dietary restrictions, religious food requirements, or a medical food-related concern, acknowledge it and transfer to in-room dining at 1921.
- If the guest asks only for menu information, opening hours, or general dining options, provide available information if known. If not known, transfer to 1921.
- If the guest wants to modify, cancel, check status of, or complain about an in-room dining order, transfer to 1921.

When speaking:
- Use natural, conversational language.
- Be courteous, attentive, and reassuring.
- Keep responses clear and concise.
- Ask one question at a time when more information is needed.
- Avoid sounding robotic or overly formal.
- Personalize recommendations based on the guest's preferences, budget, timing, group size, location, dietary needs, accessibility needs, preferred language, and special occasions.
- Confirm important details such as date, time, number of guests, destination, room number if provided, dietary restrictions, allergies, wake-up call time, and preferred language.
- Offer helpful alternatives if the first option is unavailable.

If the guest asks for something outside your ability, politely explain and offer the next best step.
Always prioritize guest comfort, clarity, safety, privacy, cultural sensitivity, and a premium hospitality experience.
Keep voice responses short enough for phone playback."""


TRANSFER_TOOL = {
    "type": "function",
    "name": "transfer_to_extension",
            "description": "Request transfer of the live call to a hotel team.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["transfer"]},
            "extension": {
                "type": "string",
                "enum": ["1920", "1921"],
                "description": "Transfer destination. Use 1920 for concierge/front desk/human support. Use 1921 for room service/in-room dining.",
            },
            "reason": {"type": "string", "description": "Short reason for transfer."},
        },
        "required": ["action", "extension", "reason"],
        "additionalProperties": False,
    },
}


@dataclass
class RealtimeTurnResult:
    transcript: str = ""
    response_text: str = ""
    response_audio_pcm24k: bytes = b""
    language: str | None = None
    transfer_action: dict[str, Any] | None = None
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
                    break

        return result

    async def _collect_transcription_before_response(
        self,
        ws: Any,
        session: CallSession,
        result: RealtimeTurnResult,
        timeout_seconds: float = 8.0,
    ) -> list[dict[str, Any]]:
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
        return (
            f"Detected caller language for this turn: {language_name} ({language_code}). "
            f"{language_response_instruction(language_code)} "
            "This turn's detected language overrides prior conversation language. "
            "If the guest explicitly asks to use another language, switch to that requested language immediately. "
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
                        "transcription": {"model": "gpt-4o-transcribe"},
                        "turn_detection": None,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": "marin",
                    },
                },
                "tools": [TRANSFER_TOOL],
                "tool_choice": "auto",
            },
        }
        await ws.send(json.dumps(payload))

        if session.history:
            context = "\n".join(
                f"{item.get('role', item.get('type', 'event'))}: {item.get('text', item.get('response', ''))}"
                for item in session.history[-8:]
            )
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
        if "transfer_to_extension" not in raw:
            return
        for key in ("arguments", "args"):
            if key in event:
                parsed = self._parse_transfer_args(event[key])
                if parsed:
                    result.transfer_action = parsed
        item = event.get("item") or event.get("response") or {}
        for output in item.get("output", []) if isinstance(item, dict) else []:
            parsed = self._parse_transfer_args(output.get("arguments", ""))
            if parsed:
                result.transfer_action = parsed

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
