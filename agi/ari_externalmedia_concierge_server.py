#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import random
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from audio_utils import OPENAI_PCM_RATE, Pcm16Resampler
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
    build_greeting_text,
    finalize_transcript_email,
    latest_unsubmitted_pending_service_request,
    notify_rainbow_transfer_transcript,
    post_wakeup_call_request,
    service_request_already_submitted,
    service_request_can_be_submitted,
    submit_service_request_notifications,
    transcript_confirms_service_request,
    transfer_target_for_extension,
)


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("ari_externalmedia_concierge")

APP_NAME = os.getenv("ARI_CONCIERGE_APP", "ari_concierge")
ARI_BASE_URL = os.getenv("ARI_BASE_URL", "http://127.0.0.1:8088/ari").rstrip("/")
ARI_USERNAME = os.getenv("ARI_USERNAME", "voicebot")
ARI_PASSWORD = os.getenv("ARI_PASSWORD", "voicebotpass")
ARI_EXTERNAL_HOST = os.getenv("ARI_EXTERNALMEDIA_HOST", "127.0.0.1")
ARI_RTP_PORT_START = int(os.getenv("ARI_EXTERNALMEDIA_RTP_PORT_START", "12000"))
ARI_RTP_PORT_END = int(os.getenv("ARI_EXTERNALMEDIA_RTP_PORT_END", "12099"))
ARI_EXTERNAL_FORMAT = os.getenv("ARI_EXTERNALMEDIA_FORMAT", "slin16")
ARI_EXTERNAL_TRANSPORT = os.getenv("ARI_EXTERNALMEDIA_TRANSPORT", "websocket").strip().lower()
RTP_SAMPLE_RATE = int(os.getenv("ARI_RTP_SAMPLE_RATE", "16000"))
RTP_FRAME_BYTES = int(os.getenv("ARI_RTP_FRAME_BYTES", str(int(RTP_SAMPLE_RATE * 20 / 1000) * 2)))
RTP_FRAME_SAMPLES = RTP_FRAME_BYTES // 2
ARI_MEDIA_WEBSOCKET_PACE_OUTPUT = os.getenv("ARI_MEDIA_WEBSOCKET_PACE_OUTPUT", "true").strip().lower() in {"1", "true", "yes", "on"}


def openai_audio_format() -> dict[str, Any]:
    media_format = ARI_EXTERNAL_FORMAT.strip().lower()
    if media_format in {"ulaw", "pcmu", "mulaw"}:
        return {"type": "audio/pcmu"}
    if media_format in {"alaw", "pcma"}:
        return {"type": "audio/pcma"}
    return {"type": "audio/pcm", "rate": OPENAI_PCM_RATE}


def uses_openai_g711() -> bool:
    return openai_audio_format()["type"] in {"audio/pcmu", "audio/pcma"}


def media_bytes_per_sample() -> int:
    media_format = ARI_EXTERNAL_FORMAT.strip().lower()
    if media_format in {"ulaw", "pcmu", "mulaw", "alaw", "pcma"}:
        return 1
    return 2


def media_silence_byte() -> bytes:
    media_format = ARI_EXTERNAL_FORMAT.strip().lower()
    if media_format in {"ulaw", "pcmu", "mulaw"}:
        return b"\xff"
    if media_format in {"alaw", "pcma"}:
        return b"\xd5"
    return b"\x00"


def _basic_auth_header() -> str:
    import base64 as b64

    token = b64.b64encode(f"{ARI_USERNAME}:{ARI_PASSWORD}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def ari_request(method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{ARI_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": _basic_auth_header(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(os.getenv("ARI_HTTP_TIMEOUT_SECONDS", "8"))) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ARI {method} {path} failed {exc.code}: {raw}") from exc


def media_ws_url(connection_id: str) -> str:
    parsed = urllib.parse.urlparse(ARI_BASE_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunparse((scheme, parsed.netloc, f"/media/{connection_id}", "", "", ""))


def ari_channel_variable(channel_id: str, variable: str) -> str | None:
    try:
        result = ari_request("GET", f"/channels/{channel_id}/variable", {"variable": variable})
    except Exception:
        LOGGER.exception("Unable to read ARI channel variable channel_id=%s variable=%s", channel_id, variable)
        return None
    value = result.get("value")
    return str(value) if value is not None else None


class RtpEndpoint(asyncio.DatagramProtocol):
    def __init__(self, session: "AriCallSession") -> None:
        self.session = session
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12:
            return
        header_len = 12 + 4 * (data[0] & 0x0F)
        if len(data) <= header_len:
            return
        payload_type = data[1] & 0x7F
        sequence = struct.unpack("!H", data[2:4])[0]
        timestamp = struct.unpack("!I", data[4:8])[0]
        ssrc = struct.unpack("!I", data[8:12])[0]
        payload = data[header_len:]
        self.session.rtp_remote = addr
        if self.session.rtp_payload_type is None:
            self.session.rtp_payload_type = payload_type
            self.session.rtp_sequence = sequence
            self.session.rtp_timestamp = timestamp
            self.session.rtp_ssrc = ssrc
            LOGGER.info("RTP learned remote call_id=%s addr=%s payload_type=%s", self.session.call_id, addr, payload_type)
        self.session.on_rtp_payload(payload)

    def send_payload(self, payload: bytes) -> None:
        if not self.transport or not self.session.rtp_remote or self.session.rtp_payload_type is None:
            return
        marker = 0
        first = 0x80
        second = marker | (self.session.rtp_payload_type & 0x7F)
        self.session.rtp_sequence = (self.session.rtp_sequence + 1) & 0xFFFF
        self.session.rtp_timestamp = (self.session.rtp_timestamp + (len(payload) // 2)) & 0xFFFFFFFF
        header = struct.pack(
            "!BBHII",
            first,
            second,
            self.session.rtp_sequence,
            self.session.rtp_timestamp,
            self.session.rtp_ssrc,
        )
        self.transport.sendto(header + payload, self.session.rtp_remote)
        self.session.rtp_packets_out += 1
        self.session.rtp_bytes_out += len(payload)
        if self.session.rtp_packets_out == 1 or self.session.rtp_packets_out % 100 == 0:
            LOGGER.info(
                "RTP sent call_id=%s packets=%d bytes=%d",
                self.session.call_id,
                self.session.rtp_packets_out,
                self.session.rtp_bytes_out,
            )


@dataclass
class AriCallSession:
    channel_id: str
    call_id: str
    channel_name: str = ""
    bridge_id: str = field(default_factory=lambda: f"bridge-{uuid.uuid4()}")
    external_channel_id: str | None = None
    media_connection_id: str | None = None
    media_ws: Any = None
    media_optimal_frame_size: int = RTP_FRAME_BYTES
    media_ready: asyncio.Event = field(default_factory=asyncio.Event)
    media_can_send: asyncio.Event = field(default_factory=asyncio.Event)
    rtp_port: int = 0
    rtp: RtpEndpoint | None = None
    rtp_remote: tuple[str, int] | None = None
    rtp_payload_type: int | None = None
    rtp_sequence: int = field(default_factory=lambda: random.randint(0, 65535))
    rtp_timestamp: int = field(default_factory=lambda: random.randint(0, 2**32 - 1))
    rtp_ssrc: int = field(default_factory=lambda: random.randint(1, 2**32 - 1))
    rtp_packets_out: int = 0
    rtp_bytes_out: int = 0
    session: CallSession = field(default_factory=lambda: CallSession(call_id=str(uuid.uuid4())))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    openai_ws: Any = None
    output_queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=200))
    output_media_buffer: bytes = b""
    input_resampler: Pcm16Resampler = field(default_factory=lambda: Pcm16Resampler(RTP_SAMPLE_RATE, OPENAI_PCM_RATE))
    output_resampler: Pcm16Resampler = field(default_factory=lambda: Pcm16Resampler(OPENAI_PCM_RATE, RTP_SAMPLE_RATE))
    last_user_transcript: str = ""
    current_response_text: str = ""
    function_call_args: dict[str, str] = field(default_factory=dict)
    function_call_names: dict[str, str] = field(default_factory=dict)
    input_audio_bytes: int = 0
    output_audio_bytes: int = 0

    def __post_init__(self) -> None:
        self.media_can_send.set()

    def on_rtp_payload(self, payload: bytes) -> None:
        if self.openai_ws is None or self.closed.is_set():
            return
        self.input_audio_bytes += len(payload)
        audio = payload if uses_openai_g711() else self.input_resampler.process(payload)
        asyncio.create_task(
            self.openai_ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio).decode("ascii"),
                    }
                )
            )
        )


class AriExternalMediaConcierge:
    def __init__(self) -> None:
        self.calls: dict[str, AriCallSession] = {}
        self.external_to_call: dict[str, str] = {}
        self.used_ports: set[int] = set()

    def allocate_port(self) -> int:
        for port in range(ARI_RTP_PORT_START, ARI_RTP_PORT_END + 1):
            if port not in self.used_ports:
                self.used_ports.add(port)
                return port
        raise RuntimeError("No ARI ExternalMedia RTP ports available")

    def release_port(self, port: int) -> None:
        self.used_ports.discard(port)

    async def run(self) -> None:
        import websockets

        ws_url = ARI_BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/events?api_key={urllib.parse.quote(f'{ARI_USERNAME}:{ARI_PASSWORD}')}&app={APP_NAME}"
        while True:
            try:
                async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
                    LOGGER.info("Connected to ARI events app=%s", APP_NAME)
                    async for raw in ws:
                        await self.handle_event(json.loads(raw))
            except Exception:
                LOGGER.exception("ARI event loop failed; reconnecting")
                await asyncio.sleep(2)

    async def handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if event_type == "StasisStart" and channel_id:
            args = event.get("args") or []
            if channel_id in self.external_to_call or channel.get("name", "").startswith(("UnicastRTP", "External")):
                return
            await self.start_call(channel, args)
        elif event_type == "StasisEnd" and channel_id:
            call = self.calls.get(channel_id)
            if call:
                await self.close_call(call, "stasis ended")

    async def start_call(self, channel: dict[str, Any], args: list[str]) -> None:
        channel_id = channel["id"]
        call_id = str(channel.get("caller", {}).get("number") or channel_id).replace("/", "_")
        caller_name = str(channel.get("caller", {}).get("name") or "")
        call = AriCallSession(channel_id=channel_id, call_id=call_id, channel_name=str(channel.get("name") or ""))
        call.session = CallSession(call_id=re_safe(channel_id), caller_id=call_id, caller_name=caller_name, preferred_language=os.getenv("DEFAULT_LANGUAGE", "en"))
        call.session.append_event("ari_concierge_started", {"channel_id": channel_id, "format": ARI_EXTERNAL_FORMAT, "transport": ARI_EXTERNAL_TRANSPORT})
        self.calls[channel_id] = call
        if ARI_EXTERNAL_TRANSPORT == "udp":
            call.rtp_port = self.allocate_port()
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(lambda: RtpEndpoint(call), local_addr=("0.0.0.0", call.rtp_port))
            call.rtp = protocol  # type: ignore[assignment]
        try:
            await asyncio.to_thread(ari_request, "POST", f"/channels/{channel_id}/answer")
            await asyncio.to_thread(ari_request, "POST", "/bridges", {"type": "mixing", "bridgeId": call.bridge_id, "name": call.bridge_id})
            await asyncio.to_thread(ari_request, "POST", f"/bridges/{call.bridge_id}/addChannel", {"channel": channel_id})
            if ARI_EXTERNAL_TRANSPORT == "websocket":
                external = await asyncio.to_thread(
                    ari_request,
                    "POST",
                    "/channels/externalMedia",
                    {
                        "app": APP_NAME,
                        "external_host": "INCOMING",
                        "format": ARI_EXTERNAL_FORMAT,
                        "encapsulation": "none",
                        "transport": "websocket",
                        "connection_type": "server",
                        "direction": "both",
                        "transport_data": "f(json)",
                    },
                )
            else:
                external = await asyncio.to_thread(
                    ari_request,
                    "POST",
                    "/channels/externalMedia",
                    {
                        "app": APP_NAME,
                        "external_host": f"{ARI_EXTERNAL_HOST}:{call.rtp_port}",
                        "format": ARI_EXTERNAL_FORMAT,
                        "encapsulation": "rtp",
                        "transport": "udp",
                        "connection_type": "client",
                        "direction": "both",
                    },
                )
            call.external_channel_id = external.get("id")
            if call.external_channel_id:
                self.external_to_call[call.external_channel_id] = channel_id
                if ARI_EXTERNAL_TRANSPORT == "websocket":
                    channel_vars = external.get("channelvars") if isinstance(external.get("channelvars"), dict) else {}
                    call.media_connection_id = channel_vars.get("MEDIA_WEBSOCKET_CONNECTION_ID")
                    if not call.media_connection_id:
                        call.media_connection_id = await asyncio.to_thread(ari_channel_variable, call.external_channel_id, "MEDIA_WEBSOCKET_CONNECTION_ID")
                    if not call.media_connection_id:
                        raise RuntimeError(f"ExternalMedia websocket channel did not expose MEDIA_WEBSOCKET_CONNECTION_ID: {external}")
                    LOGGER.info(
                        "ExternalMedia websocket ready call_id=%s external_channel_id=%s connection_id=%s",
                        call.call_id,
                        call.external_channel_id,
                        call.media_connection_id,
                    )
                    asyncio.create_task(self.run_media_websocket(call))
                    await asyncio.wait_for(call.media_ready.wait(), timeout=float(os.getenv("ARI_MEDIA_WEBSOCKET_READY_TIMEOUT_SECONDS", "4")))
                await self.add_channel_to_bridge_when_ready(call, call.external_channel_id)
            asyncio.create_task(self.run_openai(call))
        except Exception as exc:
            LOGGER.exception("ARI concierge startup failed call_id=%s", call.call_id)
            call.session.append_event("ari_concierge_error", {"error": str(exc)})
            await self.close_call(call, "startup failed")

    async def add_channel_to_bridge_when_ready(self, call: AriCallSession, channel_id: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 11):
            try:
                await asyncio.to_thread(ari_request, "POST", f"/bridges/{call.bridge_id}/addChannel", {"channel": channel_id})
                return
            except Exception as exc:
                last_error = exc
                if "Channel not in Stasis application" not in str(exc):
                    raise
                LOGGER.info(
                    "Bridge add waiting for Stasis call_id=%s channel_id=%s attempt=%d",
                    call.call_id,
                    channel_id,
                    attempt,
                )
                await asyncio.sleep(0.2)
        raise RuntimeError(f"Unable to add channel {channel_id} to bridge after waiting: {last_error}")

    async def run_media_websocket(self, call: AriCallSession) -> None:
        import websockets

        if not call.media_connection_id:
            return
        try:
            async with websockets.connect(media_ws_url(call.media_connection_id), max_size=20 * 1024 * 1024) as ws:
                call.media_ws = ws
                LOGGER.info("Media websocket connected call_id=%s connection_id=%s", call.call_id, call.media_connection_id)
                async for message in ws:
                    if isinstance(message, bytes):
                        call.on_rtp_payload(message)
                    else:
                        self.handle_media_control(call, str(message))
        except Exception as exc:
            if not call.closed.is_set():
                LOGGER.exception("Media websocket failed call_id=%s", call.call_id)
                call.session.append_event("ari_media_websocket_error", {"error": str(exc)})
        finally:
            call.media_ws = None
            await self.close_call(call, "media websocket ended")

    def handle_media_control(self, call: AriCallSession, message: str) -> None:
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            event_name = message.split(" ", 1)[0]
            if event_name == "MEDIA_START":
                for part in message.split():
                    if part.startswith("optimal_frame_size:"):
                        with contextlib.suppress(ValueError):
                            call.media_optimal_frame_size = int(part.split(":", 1)[1])
                call.media_ready.set()
            elif event_name == "MEDIA_XOFF":
                call.media_can_send.clear()
            elif event_name == "MEDIA_XON":
                call.media_can_send.set()
            call.session.append_event("ari_media_websocket_event", {"event": event_name, "raw": message[:300]})
            return
        if event.get("event") == "MEDIA_START":
            optimal = event.get("optimal_frame_size")
            if isinstance(optimal, int) and optimal > 0:
                call.media_optimal_frame_size = optimal
            call.media_ready.set()
            call.session.append_event(
                "ari_media_websocket_start",
                {
                    "format": event.get("format"),
                    "optimal_frame_size": event.get("optimal_frame_size"),
                    "ptime": event.get("ptime"),
                },
            )
        elif event.get("event") == "MEDIA_XOFF":
            call.media_can_send.clear()
            call.session.append_event("ari_media_websocket_event", {"event": "MEDIA_XOFF"})
        elif event.get("event") == "MEDIA_XON":
            call.media_can_send.set()
            call.session.append_event("ari_media_websocket_event", {"event": "MEDIA_XON"})
        elif event.get("event"):
            call.session.append_event("ari_media_websocket_event", {"event": event.get("event")})

    def session_update_payload(self) -> dict[str, Any]:
        audio_format = openai_audio_format()
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime"),
                "instructions": ASSISTANT_INSTRUCTIONS,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": audio_format,
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
                        "format": audio_format,
                        "voice": os.getenv("OPENAI_REALTIME_VOICE", "marin"),
                    },
                },
                "tools": [TRANSFER_TOOL, SUBMIT_HOTEL_REQUEST_TOOL, END_CALL_TOOL],
                "tool_choice": "auto",
            },
        }

    async def run_openai(self, call: AriCallSession) -> None:
        import websockets

        url = f"{REALTIME_URL}?model={os.getenv('OPENAI_REALTIME_MODEL', 'gpt-realtime')}"
        headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}"}
        try:
            async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
                call.openai_ws = ws
                await ws.send(json.dumps(self.session_update_payload()))
                greeting_text, _personalized = build_greeting_text(call.session)
                await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"], "instructions": greeting_text}}))
                if ARI_EXTERNAL_TRANSPORT == "websocket":
                    await asyncio.gather(self.openai_to_rtp(call, ws), self.websocket_playout(call))
                else:
                    await asyncio.gather(self.openai_to_rtp(call, ws), self.rtp_playout(call))
        except Exception as exc:
            if not call.closed.is_set():
                LOGGER.exception("OpenAI loop failed call_id=%s", call.call_id)
                call.session.append_event("ari_openai_error", {"error": str(exc)})
        finally:
            await self.close_call(call, "openai loop ended")

    async def openai_to_rtp(self, call: AriCallSession, ws: Any) -> None:
        while not call.closed.is_set():
            event = json.loads(await ws.recv())
            event_type = event.get("type", "")
            if event_type in {"response.audio.delta", "response.output_audio.delta"}:
                audio = base64.b64decode(event.get("delta", ""))
                call.output_audio_bytes += len(audio)
                await self.queue_output(call, audio if uses_openai_g711() else call.output_resampler.process(audio))
            elif event_type in {"input_audio_buffer.speech_started", "conversation.input_audio_buffer.speech_started"}:
                self.clear_output(call)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = str(event.get("transcript") or "").strip()
                if transcript:
                    call.last_user_transcript = transcript
                    language, confidence = detect_language_from_text(transcript, call.session.preferred_language)
                    call.session.update_language(language, confidence, source="ari_externalmedia_transcription")
                    call.session.append_event("user", {"role": "user", "text": transcript, "language": call.session.preferred_language})
                    await self.maybe_submit_pending_service_request(call, transcript)
            elif event_type in {"response.audio_transcript.delta", "response.output_audio_transcript.delta", "response.text.delta", "response.output_text.delta"}:
                call.current_response_text += event.get("delta", "")
            elif event_type in {"response.function_call_arguments.delta", "response.tool_call_arguments.delta"}:
                call_id = event.get("call_id") or event.get("item_id") or "default"
                call.function_call_args[call_id] = call.function_call_args.get(call_id, "") + event.get("delta", "")
            elif event_type in {"response.output_item.done", "conversation.item.created"}:
                self.capture_tool_metadata(call, event)
            elif event_type == "response.done":
                if call.current_response_text:
                    call.session.append_event("assistant", {"role": "assistant", "text": call.current_response_text})
                    call.current_response_text = ""
                await self.handle_completed_tool_calls(call, ws)
            elif event_type == "error":
                call.session.append_event("openai_error", {"error": event.get("error", event)})

    async def queue_output(self, call: AriCallSession, pcm24k: bytes) -> None:
        if call.output_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                call.output_queue.get_nowait()
                call.output_queue.task_done()
        await call.output_queue.put(pcm24k)

    def clear_output(self, call: AriCallSession) -> None:
        while True:
            try:
                call.output_queue.get_nowait()
                call.output_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def rtp_playout(self, call: AriCallSession) -> None:
        frame_seconds = RTP_FRAME_SAMPLES / RTP_SAMPLE_RATE
        while not call.closed.is_set() or not call.output_queue.empty():
            try:
                pcm = await asyncio.wait_for(call.output_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            for offset in range(0, len(pcm), RTP_FRAME_BYTES):
                chunk = pcm[offset : offset + RTP_FRAME_BYTES]
                if len(chunk) < RTP_FRAME_BYTES:
                    chunk = chunk + (b"\x00" * (RTP_FRAME_BYTES - len(chunk)))
                if call.rtp:
                    call.rtp.send_payload(chunk)
                await asyncio.sleep(frame_seconds)
            call.output_queue.task_done()

    async def websocket_playout(self, call: AriCallSession) -> None:
        fallback_frame_seconds = RTP_FRAME_SAMPLES / RTP_SAMPLE_RATE
        while not call.closed.is_set() or not call.output_queue.empty():
            try:
                pcm = await asyncio.wait_for(call.output_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                if call.closed.is_set() and call.output_media_buffer:
                    await self.send_media_websocket_frame(call, call.output_media_buffer, pad=True)
                    call.output_media_buffer = b""
                continue
            while not call.media_ws and not call.closed.is_set():
                await asyncio.sleep(0.02)
            frame_size = max(1, call.media_optimal_frame_size or RTP_FRAME_BYTES)
            if call.media_ws:
                call.output_media_buffer += pcm
                while len(call.output_media_buffer) >= frame_size:
                    chunk = call.output_media_buffer[:frame_size]
                    call.output_media_buffer = call.output_media_buffer[frame_size:]
                    await self.send_media_websocket_frame(call, chunk)
            call.output_queue.task_done()
        if call.media_ws and call.output_media_buffer:
            await self.send_media_websocket_frame(call, call.output_media_buffer, pad=True)
            call.output_media_buffer = b""

    async def send_media_websocket_frame(self, call: AriCallSession, chunk: bytes, pad: bool = False) -> None:
        if not call.media_ws:
            return
        frame_size = max(1, call.media_optimal_frame_size or RTP_FRAME_BYTES)
        if pad and len(chunk) < frame_size:
            chunk = chunk + (media_silence_byte() * (frame_size - len(chunk)))
        while not call.media_can_send.is_set() and not call.closed.is_set():
            await asyncio.sleep(0.02)
        await call.media_ws.send(chunk)
        call.rtp_packets_out += 1
        call.rtp_bytes_out += len(chunk)
        if call.rtp_packets_out == 1 or call.rtp_packets_out % 100 == 0:
            LOGGER.info(
                "Media websocket sent call_id=%s packets=%d bytes=%d frame_size=%d",
                call.call_id,
                call.rtp_packets_out,
                call.rtp_bytes_out,
                frame_size,
            )
        if ARI_MEDIA_WEBSOCKET_PACE_OUTPUT:
            frame_seconds = max(0.005, len(chunk) / (RTP_SAMPLE_RATE * media_bytes_per_sample())) if RTP_SAMPLE_RATE > 0 else 0.02
            await asyncio.sleep(frame_seconds)

    def capture_tool_metadata(self, call: AriCallSession, event: dict[str, Any]) -> None:
        item = event.get("item") or {}
        if not isinstance(item, dict):
            return
        if item.get("type") in {"function_call", "tool_call"} and item.get("name"):
            call_id = str(item.get("call_id") or item.get("id") or item.get("item_id") or "default")
            call.function_call_names[call_id] = str(item.get("name"))
            if item.get("arguments"):
                call.function_call_args[call_id] = str(item.get("arguments"))

    async def handle_completed_tool_calls(self, call: AriCallSession, ws: Any) -> None:
        for call_id, raw_args in list(call.function_call_args.items()):
            parsed = self.parse_args(raw_args)
            name = call.function_call_names.get(call_id, "")
            if not parsed:
                continue
            if name == "transfer_to_extension" or parsed.get("action") == "transfer":
                await self.transfer_call(call, parsed)
            elif name == "end_call" or parsed.get("action") == "end_call":
                await self.hangup_call(call, parsed.get("reason", "guest ended the call"))
            elif name == "submit_hotel_request" or parsed.get("category"):
                await self.handle_service_request(call, ws, call_id, parsed)
            call.function_call_args.pop(call_id, None)
            call.function_call_names.pop(call_id, None)

    async def handle_service_request(self, call: AriCallSession, ws: Any, tool_call_id: str, payload: dict[str, Any]) -> None:
        payload = apply_known_room_number(call.session, payload) or payload
        duplicate, submitted_request = service_request_already_submitted(call.session, payload)
        if duplicate:
            await self.send_tool_output(ws, tool_call_id, {"submitted": True, "duplicate": True, "request": submitted_request})
            return
        can_submit, reason = service_request_can_be_submitted(payload, call.last_user_transcript)
        if not can_submit:
            call.session.append_event("service_request_confirmation_required", {"reason": reason, "request": payload})
            await self.send_tool_output(ws, tool_call_id, {"submitted": False, "needs_confirmation": True, "reason": reason, "request": payload})
            await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"], "instructions": "Ask the guest to explicitly confirm before submitting the request."}}))
            return
        await self.submit_service_request(call, payload, ws, tool_call_id)

    async def maybe_submit_pending_service_request(self, call: AriCallSession, transcript: str) -> None:
        if not transcript_confirms_service_request(transcript):
            return
        pending = latest_unsubmitted_pending_service_request(call.session)
        if not pending or call.openai_ws is None:
            return
        call.session.append_event("service_request_pending_confirmed", {"transcript": transcript, "request": pending})
        await self.submit_service_request(call, pending, call.openai_ws, None)

    async def submit_service_request(self, call: AriCallSession, payload: dict[str, Any], ws: Any, tool_call_id: str | None) -> None:
        submission, rainbow_submission = await submit_service_request_notifications(call.session, payload)
        call.session.append_event("service_request_submitted", submission)
        if (payload.get("category") or "").strip().lower() == "wake_up_call":
            wakeup_result = await asyncio.to_thread(post_wakeup_call_request, call.session, payload)
            call.session.append_event("wakeup_call_result", wakeup_result)
        call.session.append_event("service_request_rainbow_result", rainbow_submission)
        if tool_call_id:
            await self.send_tool_output(ws, tool_call_id, {"submitted": bool(submission.get("sent")), "request": payload})
        await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"], "instructions": "Briefly tell the guest the request has been submitted, then ask if there is anything else."}}))

    async def send_tool_output(self, ws: Any, call_id: str, output: dict[str, Any]) -> None:
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(output, ensure_ascii=False)}}))

    async def transfer_call(self, call: AriCallSession, action: dict[str, Any]) -> None:
        transfer_extension = os.getenv("TRANSFER_EXTENSION", os.getenv("HUMAN_TRANSFER_EXTENSION", "1920"))
        if action.get("transfer_type") == "room_service":
            transfer_extension = os.getenv("ROOM_SERVICE_TRANSFER_EXTENSION", "1921")
        extension = str(action.get("extension") or transfer_extension)
        call.session.append_event("transfer_requested", {"reason": action.get("reason", "transfer requested"), "extension": extension, "transfer_type": action.get("transfer_type", "human")})
        try:
            result = await asyncio.to_thread(notify_rainbow_transfer_transcript, call.session, action.get("transfer_type", "human"), extension)
            call.session.append_event("transfer_transcript_rainbow_result", result)
        except Exception as exc:
            call.session.append_event("transfer_transcript_rainbow_error", {"error": str(exc)})
        transfer_target = transfer_target_for_extension(extension)
        try:
            await asyncio.to_thread(
                ari_request,
                "POST",
                f"/channels/{call.channel_id}/variable",
                {
                    "variable": "AI_TRANSFER_TARGET",
                    "value": transfer_target,
                },
            )
            await asyncio.to_thread(
                ari_request,
                "POST",
                f"/channels/{call.channel_id}/variable",
                {
                    "variable": "AI_TRANSFER_EXTENSION",
                    "value": extension,
                },
            )
            await asyncio.to_thread(
                ari_request,
                "POST",
                f"/channels/{call.channel_id}/continue",
                {
                    "context": "ari-sip-transfer",
                    "extension": "refer",
                    "priority": 1,
                },
            )
            call.session.append_event(
                "transfer_result",
                {"extension": extension, "target": transfer_target, "status": "refer_requested"},
            )
        except Exception as exc:
            call.session.append_event("transfer_result", {"extension": extension, "target": transfer_target, "status": "failed", "error": str(exc)})
            await self.close_call(call, "transfer failed")

    async def hangup_call(self, call: AriCallSession, reason: str) -> None:
        call.session.append_event("call_closing", {"reason": reason})
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ari_request, "DELETE", f"/channels/{call.channel_id}")
        await self.close_call(call, reason)

    def parse_args(self, raw_args: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def close_call(self, call: AriCallSession, reason: str) -> None:
        if call.closed.is_set():
            return
        call.closed.set()
        call.session.append_event(
            "ari_concierge_closed",
            {
                "reason": reason,
                "input_audio_bytes": call.input_audio_bytes,
                "output_audio_bytes": call.output_audio_bytes,
                "rtp_packets_out": call.rtp_packets_out,
                "rtp_bytes_out": call.rtp_bytes_out,
            },
        )
        finalize_transcript_email(call.session)
        with contextlib.suppress(Exception):
            if call.external_channel_id:
                await asyncio.to_thread(ari_request, "DELETE", f"/channels/{call.external_channel_id}")
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ari_request, "DELETE", f"/bridges/{call.bridge_id}")
        with contextlib.suppress(Exception):
            if call.media_ws:
                await call.media_ws.close()
        if call.rtp and call.rtp.transport:
            call.rtp.transport.close()
        if call.rtp_port:
            self.release_port(call.rtp_port)
        self.calls.pop(call.channel_id, None)
        if call.external_channel_id:
            self.external_to_call.pop(call.external_channel_id, None)


def re_safe(value: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


async def main() -> None:
    server = AriExternalMediaConcierge()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
