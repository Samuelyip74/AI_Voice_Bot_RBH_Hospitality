#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from audiosocket_translation_server import (
    AUDIOSOCKET_KIND_AUDIO_8K,
    AUDIOSOCKET_KIND_DTMF,
    AUDIOSOCKET_KIND_ERROR,
    AUDIOSOCKET_KIND_HANGUP,
    AUDIOSOCKET_KIND_UUID,
    audiosocket_uuid,
    read_audiosocket_packet,
    write_audiosocket_packet,
)
from audio_utils import OPENAI_PCM_RATE, resample_pcm16
from call_session import CallSession, detect_language_from_text
from openai_realtime_client import (
    ASSISTANT_INSTRUCTIONS,
    END_CALL_TOOL,
    REALTIME_URL,
    SUBMIT_HOTEL_REQUEST_TOOL,
    TRANSFER_TOOL,
)
from voice_assistant_eagi import (
    apply_known_room_number,
    latest_unsubmitted_pending_service_request,
    post_wakeup_call_request,
    service_request_already_submitted,
    service_request_can_be_submitted,
    transcript_confirms_service_request,
    submit_service_request_notifications,
)


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("audiosocket_concierge_server")


def concierge_session_dir() -> Path:
    return Path(os.getenv("CONCIERGE_SESSION_DIR", "/var/log/asterisk/ai/concierge_sessions"))


def concierge_action_dir() -> Path:
    return Path(os.getenv("CONCIERGE_ACTION_DIR", "/var/log/asterisk/ai/concierge_actions"))


class AudioSocketConciergeSession:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.closed = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.call_uuid = "unknown"
        self.input_rate = 8000
        self.output_rate = 8000
        self.output_frame_bytes = int(os.getenv("CONCIERGE_OUTPUT_FRAME_BYTES", "320"))
        self.output_pacing_enabled = os.getenv("CONCIERGE_OUTPUT_PACING", "true").lower() == "true"
        self.output_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=int(os.getenv("CONCIERGE_OUTPUT_QUEUE_MAX_CHUNKS", "200"))
        )
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
        self.session = CallSession(call_id="unknown", preferred_language=os.getenv("DEFAULT_LANGUAGE", "en"))
        self.audio_packets_in = 0
        self.audio_bytes_in = 0
        self.audio_deltas_out = 0
        self.audio_bytes_out = 0
        self.function_call_args: dict[str, str] = {}
        self.function_call_names: dict[str, str] = {}
        self.last_user_transcript = ""
        self.current_response_text = ""

    async def run(self) -> None:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")
        import websockets

        peer = self.writer.get_extra_info("peername")
        await self._read_initial_uuid()
        if self.closed.is_set():
            return
        self._load_session_for_uuid()
        LOGGER.info("AudioSocket concierge connection from %s uuid=%s call_id=%s", peer, self.call_uuid, self.session.call_id)
        url = f"{REALTIME_URL}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
            await ws.send(json.dumps(self._session_update_payload()))
            await self._send_start_context(ws)
            if os.getenv("CONCIERGE_SERVER_GREETING_ENABLED", "false").lower() == "true":
                await ws.send(
                    json.dumps(
                        {
                            "type": "response.create",
                            "response": {
                                "output_modalities": ["audio"],
                                "instructions": "Greet the caller briefly as Nova and ask how you may help.",
                            },
                        }
                    )
                )
            LOGGER.info("OpenAI concierge session started uuid=%s", self.call_uuid)
            await asyncio.gather(self._asterisk_to_openai(ws), self._openai_to_asterisk(ws), self._playout_to_asterisk())

    async def _read_initial_uuid(self) -> None:
        packet = await read_audiosocket_packet(self.reader)
        if packet is None:
            self.closed.set()
            return
        kind, payload = packet
        if kind == AUDIOSOCKET_KIND_UUID:
            self.call_uuid = audiosocket_uuid(payload)
            return
        if kind == AUDIOSOCKET_KIND_HANGUP:
            self.closed.set()

    def _load_session_for_uuid(self) -> None:
        session_file = concierge_session_dir() / f"{self.call_uuid}.json"
        if not session_file.exists():
            self.session = CallSession(call_id=self.call_uuid, preferred_language=os.getenv("DEFAULT_LANGUAGE", "en"))
            return
        data = json.loads(session_file.read_text(encoding="utf-8"))
        self.session = CallSession(
            call_id=str(data.get("call_id") or self.call_uuid),
            caller_id=str(data.get("caller_id") or ""),
            caller_name=str(data.get("caller_name") or ""),
            room_number=str(data.get("room_number") or ""),
            sip_from_header=str(data.get("sip_from_header") or ""),
            preferred_language=str(data.get("preferred_language") or os.getenv("DEFAULT_LANGUAGE", "en")),
            detected_language=str(data.get("preferred_language") or os.getenv("DEFAULT_LANGUAGE", "en")),
        )
        self.session.append_event(
            "full_duplex_concierge_started",
            {"audiosocket_uuid": self.call_uuid, "model": self.model},
        )

    def _session_update_payload(self) -> dict[str, Any]:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.model,
                "instructions": ASSISTANT_INSTRUCTIONS,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": os.getenv("OPENAI_REALTIME_TRANSCRIPTION_MODEL", "gpt-4o-transcribe")},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": float(os.getenv("CONCIERGE_VAD_THRESHOLD", "0.55")),
                            "prefix_padding_ms": int(os.getenv("CONCIERGE_VAD_PREFIX_PADDING_MS", "300")),
                            "silence_duration_ms": int(os.getenv("CONCIERGE_VAD_SILENCE_DURATION_MS", "650")),
                            "create_response": True,
                            "interrupt_response": True,
                        },
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

    async def _send_start_context(self, ws: Any) -> None:
        lines = [
            f"Caller ID: {self.session.caller_id}",
            f"Caller name: {self.session.caller_name}",
            f"Known caller room number: {self.session.room_number}",
            f"Current preferred language: {self.session.preferred_language}",
            "The call is full-duplex. Keep responses concise and allow interruption.",
        ]
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": "\n".join(lines)}],
                    },
                }
            )
        )

    async def _asterisk_to_openai(self, ws: Any) -> None:
        input_start_delay_ms = int(os.getenv("CONCIERGE_INPUT_START_DELAY_MS", "700"))
        accept_audio_at = time.monotonic() + (input_start_delay_ms / 1000)
        dropped_startup_packets = 0
        try:
            while not self.closed.is_set():
                packet = await read_audiosocket_packet(self.reader)
                if packet is None:
                    break
                kind, payload = packet
                if kind == AUDIOSOCKET_KIND_AUDIO_8K:
                    self.audio_packets_in += 1
                    self.audio_bytes_in += len(payload)
                    if self.audio_packets_in == 1 or self.audio_packets_in % 100 == 0:
                        LOGGER.info("Concierge audio in uuid=%s packets=%d bytes=%d", self.call_uuid, self.audio_packets_in, self.audio_bytes_in)
                    if time.monotonic() < accept_audio_at:
                        dropped_startup_packets += 1
                        continue
                    if dropped_startup_packets:
                        LOGGER.info(
                            "Concierge startup audio gate released uuid=%s dropped_packets=%d delay_ms=%d",
                            self.call_uuid,
                            dropped_startup_packets,
                            input_start_delay_ms,
                        )
                        dropped_startup_packets = 0
                    pcm24k = resample_pcm16(payload, self.input_rate, OPENAI_PCM_RATE)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm24k).decode("ascii"),
                            }
                        )
                    )
                elif kind == AUDIOSOCKET_KIND_DTMF:
                    if payload in {b"#", b"*"}:
                        await self._write_action({"action": "end_call", "reason": "caller pressed DTMF to end full-duplex concierge"})
                        break
                elif kind in {AUDIOSOCKET_KIND_HANGUP, AUDIOSOCKET_KIND_ERROR}:
                    break
        except Exception:
            LOGGER.exception("Concierge AudioSocket receive loop failed uuid=%s", self.call_uuid)
        finally:
            self.closed.set()
            self.session.append_event("full_duplex_audio_in_closed", {"packets": self.audio_packets_in, "bytes": self.audio_bytes_in})
            with contextlib.suppress(Exception):
                await ws.close()

    async def _openai_to_asterisk(self, ws: Any) -> None:
        try:
            while not self.closed.is_set():
                event = json.loads(await ws.recv())
                event_type = event.get("type", "")
                if event_type in {"response.audio.delta", "response.output_audio.delta"}:
                    pcm24k = base64.b64decode(event.get("delta", ""))
                    pcm8k = resample_pcm16(pcm24k, OPENAI_PCM_RATE, self.output_rate)
                    self.audio_deltas_out += 1
                    self.audio_bytes_out += len(pcm8k)
                    if self.audio_deltas_out == 1 or self.audio_deltas_out % 20 == 0:
                        LOGGER.info("Concierge audio out uuid=%s deltas=%d bytes=%d", self.call_uuid, self.audio_deltas_out, self.audio_bytes_out)
                    await self._queue_pcm_for_playout(pcm8k)
                elif event_type in {"input_audio_buffer.speech_started", "conversation.input_audio_buffer.speech_started"}:
                    self._clear_output_queue()
                elif event_type in {"conversation.item.input_audio_transcription.completed"}:
                    transcript = str(event.get("transcript") or "").strip()
                    if transcript:
                        self.last_user_transcript = transcript
                        language, confidence = detect_language_from_text(transcript, self.session.preferred_language)
                        self.session.update_language(language, confidence, source="full_duplex_transcription")
                        self.session.append_event("user", {"role": "user", "text": transcript, "language": self.session.preferred_language})
                        await self._maybe_submit_pending_service_request(ws, transcript)
                elif event_type in {"response.audio_transcript.delta", "response.output_audio_transcript.delta", "response.text.delta", "response.output_text.delta"}:
                    self.current_response_text += event.get("delta", "")
                elif event_type in {"response.function_call_arguments.delta", "response.tool_call_arguments.delta"}:
                    call_id = event.get("call_id") or event.get("item_id") or "default"
                    self.function_call_args[call_id] = self.function_call_args.get(call_id, "") + event.get("delta", "")
                elif event_type in {"response.output_item.done", "conversation.item.created"}:
                    self._capture_tool_metadata(event)
                elif event_type == "response.done":
                    if self.current_response_text:
                        self.session.append_event("assistant", {"role": "assistant", "text": self.current_response_text})
                        self.current_response_text = ""
                    await self._handle_completed_tool_calls(ws)
                elif event_type == "error":
                    LOGGER.error("OpenAI concierge error uuid=%s error=%s", self.call_uuid, event.get("error", event))
        except Exception:
            if not self.closed.is_set():
                LOGGER.exception("Concierge OpenAI receive loop failed uuid=%s", self.call_uuid)
        finally:
            self.closed.set()
            self.session.append_event("full_duplex_audio_out_closed", {"deltas": self.audio_deltas_out, "bytes": self.audio_bytes_out})

    def _capture_tool_metadata(self, event: dict[str, Any]) -> None:
        item = event.get("item") or {}
        if not isinstance(item, dict):
            return
        if item.get("type") in {"function_call", "tool_call"} and item.get("name"):
            call_id = item.get("call_id") or item.get("id") or item.get("item_id") or "default"
            self.function_call_names[str(call_id)] = str(item.get("name"))
            if item.get("arguments"):
                self.function_call_args[str(call_id)] = str(item.get("arguments"))

    async def _handle_completed_tool_calls(self, ws: Any) -> None:
        for call_id, raw_args in list(self.function_call_args.items()):
            name = self.function_call_names.get(call_id, "")
            parsed = self._parse_args(raw_args)
            if not parsed:
                continue
            if name == "transfer_to_extension" or parsed.get("action") == "transfer":
                await self._write_action(parsed)
                self.closed.set()
            elif name == "end_call" or parsed.get("action") == "end_call":
                await self._write_action(parsed)
                self.closed.set()
            elif name == "submit_hotel_request" or parsed.get("category"):
                await self._handle_service_request(ws, call_id, parsed)
            self.function_call_args.pop(call_id, None)
            self.function_call_names.pop(call_id, None)

    async def _handle_service_request(self, ws: Any, call_id: str, payload: dict[str, Any]) -> None:
        payload = apply_known_room_number(self.session, payload) or payload
        duplicate, submitted_request = service_request_already_submitted(self.session, payload)
        if duplicate:
            await self._send_tool_output(ws, call_id, {"submitted": True, "duplicate": True, "request": submitted_request})
            return
        can_submit, reason = service_request_can_be_submitted(payload, self.last_user_transcript)
        if not can_submit:
            self.session.append_event("service_request_confirmation_required", {"reason": reason, "request": payload})
            await self._send_tool_output(ws, call_id, {"submitted": False, "needs_confirmation": True, "reason": reason, "request": payload})
            await self._ask_for_explicit_confirmation(ws, payload)
            return
        await self._submit_service_request(ws, payload, call_id=call_id)

    async def _maybe_submit_pending_service_request(self, ws: Any, transcript: str) -> None:
        if not transcript_confirms_service_request(transcript):
            return
        pending_request = latest_unsubmitted_pending_service_request(self.session)
        if not pending_request:
            return
        pending_request = apply_known_room_number(self.session, pending_request) or pending_request
        self.session.append_event(
            "service_request_pending_confirmed",
            {"transcript": transcript, "request": pending_request},
        )
        await self._submit_service_request(ws, pending_request)

    async def _submit_service_request(self, ws: Any, payload: dict[str, Any], call_id: str | None = None) -> None:
        duplicate, submitted_request = service_request_already_submitted(self.session, payload)
        if duplicate:
            if call_id:
                await self._send_tool_output(ws, call_id, {"submitted": True, "duplicate": True, "request": submitted_request})
            return
        submission, rainbow_submission = await submit_service_request_notifications(self.session, payload)
        self.session.append_event("service_request_submitted", submission)
        if (payload.get("category") or "").strip().lower() == "wake_up_call":
            wakeup_result = await asyncio.to_thread(post_wakeup_call_request, self.session, payload)
            self.session.append_event("wakeup_call_result", wakeup_result)
        self.session.append_event("service_request_rainbow_result", rainbow_submission)
        if call_id:
            await self._send_tool_output(ws, call_id, {"submitted": bool(submission.get("sent")), "request": payload})
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "output_modalities": ["audio"],
                        "instructions": "Briefly tell the guest the request has been submitted, then ask if there is anything else.",
                    },
                }
            )
        )

    async def _send_tool_output(self, ws: Any, call_id: str, output: dict[str, Any]) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(output, ensure_ascii=False),
                    },
                }
            )
        )

    async def _ask_for_explicit_confirmation(self, ws: Any, payload: dict[str, Any]) -> None:
        summary = str(payload.get("summary") or "the request").strip()
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "output_modalities": ["audio"],
                        "instructions": (
                            "Ask the guest to explicitly confirm before submitting. "
                            f"Summarize the pending request briefly: {summary}."
                        ),
                    },
                }
            )
        )

    def _parse_args(self, raw_args: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def _write_action(self, action: dict[str, Any]) -> None:
        concierge_action_dir().mkdir(parents=True, exist_ok=True)
        action_file = concierge_action_dir() / f"{self.call_uuid}.json"
        action_file.write_text(json.dumps(action, ensure_ascii=False), encoding="utf-8")
        self.session.append_event("full_duplex_action_requested", {"audiosocket_uuid": self.call_uuid, "action": action})

    async def _queue_pcm_for_playout(self, pcm8k: bytes) -> None:
        if self.output_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.output_queue.get_nowait()
                self.output_queue.task_done()
        await self.output_queue.put(pcm8k)

    def _clear_output_queue(self) -> None:
        cleared = 0
        while True:
            try:
                self.output_queue.get_nowait()
                self.output_queue.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        if cleared:
            LOGGER.info("Concierge output queue cleared for barge-in uuid=%s chunks=%d", self.call_uuid, cleared)

    async def _playout_to_asterisk(self) -> None:
        frame_bytes = max(2, self.output_frame_bytes - (self.output_frame_bytes % 2))
        frame_seconds = frame_bytes / (self.output_rate * 2)
        try:
            while not self.closed.is_set() or not self.output_queue.empty():
                try:
                    pcm8k = await asyncio.wait_for(self.output_queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                async with self.write_lock:
                    for offset in range(0, len(pcm8k), frame_bytes):
                        chunk = pcm8k[offset : offset + frame_bytes]
                        if not chunk:
                            continue
                        await write_audiosocket_packet(self.writer, AUDIOSOCKET_KIND_AUDIO_8K, chunk)
                        if self.output_pacing_enabled:
                            await asyncio.sleep(frame_seconds)
                self.output_queue.task_done()
        except Exception:
            if not self.closed.is_set():
                LOGGER.exception("Concierge playout loop failed uuid=%s", self.call_uuid)
        finally:
            self.closed.set()

    async def close(self) -> None:
        self.closed.set()
        with contextlib.suppress(Exception):
            await write_audiosocket_packet(self.writer, AUDIOSOCKET_KIND_HANGUP)
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    session = AudioSocketConciergeSession(reader, writer)
    try:
        await session.run()
    finally:
        await session.close()


async def main_async() -> None:
    host = os.getenv("CONCIERGE_AUDIOSOCKET_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("CONCIERGE_AUDIOSOCKET_PORT", "9020"))
    server = await asyncio.start_server(handle_connection, host, port)
    LOGGER.info("AudioSocket concierge server listening on %s", ", ".join(str(sock.getsockname()) for sock in server.sockets or []))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)
    async with server:
        await stop_event.wait()
        server.close()
        await server.wait_closed()


def main() -> int:
    try:
        asyncio.run(main_async())
        return 0
    except Exception:
        LOGGER.exception("Fatal AudioSocket concierge server failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
