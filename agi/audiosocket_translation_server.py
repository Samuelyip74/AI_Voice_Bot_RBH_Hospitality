#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import struct
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from audio_utils import OPENAI_PCM_RATE, Pcm16Resampler
from openai_realtime_translation_client import (
    REALTIME_TRANSLATION_URL,
    OpenAIRealtimeTranslationClient,
    normalize_translation_language,
)


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("audiosocket_translation_server")


AUDIOSOCKET_KIND_HANGUP = 0x00
AUDIOSOCKET_KIND_UUID = 0x01
AUDIOSOCKET_KIND_DTMF = 0x03
AUDIOSOCKET_KIND_AUDIO_8K = 0x10
AUDIOSOCKET_KIND_ERROR = 0xFF


async def read_audiosocket_packet(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        header = await reader.readexactly(3)
    except asyncio.IncompleteReadError:
        return None
    kind = header[0]
    length = struct.unpack("!H", header[1:3])[0]
    payload = b""
    if length:
        try:
            payload = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            return None
    return kind, payload


async def write_audiosocket_packet(writer: asyncio.StreamWriter, kind: int, payload: bytes = b"") -> None:
    for offset in range(0, len(payload) or 1, 64000):
        chunk = payload[offset : offset + 64000] if payload else b""
        writer.write(bytes([kind]) + struct.pack("!H", len(chunk)) + chunk)
        await writer.drain()
        if not payload:
            break


def audiosocket_uuid(payload: bytes) -> str:
    if len(payload) == 16:
        import uuid

        return str(uuid.UUID(bytes=payload))
    return payload.decode("utf-8", errors="replace")


class AudioSocketTranslationSession:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.closed = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.call_uuid = "unknown"
        self.target_language = normalize_translation_language(
            os.getenv("TRANSLATION_TARGET_LANGUAGE", os.getenv("TRANSLATION_DEFAULT_TARGET_LANGUAGE", "en"))
        )
        self.input_rate = 8000
        self.output_rate = 8000
        self.input_resampler = Pcm16Resampler(self.input_rate, OPENAI_PCM_RATE)
        self.output_resampler = Pcm16Resampler(OPENAI_PCM_RATE, self.output_rate)
        self.output_frame_bytes = int(os.getenv("TRANSLATION_OUTPUT_FRAME_BYTES", "320"))
        self.output_pacing_enabled = os.getenv("TRANSLATION_OUTPUT_PACING", "true").lower() == "true"
        self.client = OpenAIRealtimeTranslationClient()
        self.audio_packets_in = 0
        self.audio_bytes_in = 0
        self.audio_deltas_out = 0
        self.audio_bytes_out = 0

    async def run(self) -> None:
        import websockets

        peer = self.writer.get_extra_info("peername")
        await self._read_initial_uuid()
        if self.closed.is_set():
            return
        self._load_target_language_for_uuid()
        LOGGER.info("AudioSocket translation connection from %s uuid=%s target=%s", peer, self.call_uuid, self.target_language)
        url = f"{REALTIME_TRANSLATION_URL}?model={self.client.model}"
        headers = {"Authorization": f"Bearer {self.client.api_key}"}
        async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
            await ws.send(json.dumps(self.client.session_update_payload(self.target_language, source_rate=OPENAI_PCM_RATE)))
            LOGGER.info("OpenAI translation session update sent uuid=%s target=%s", self.call_uuid, self.target_language)
            await asyncio.gather(self._asterisk_to_openai(ws), self._openai_to_asterisk(ws))

    async def _read_initial_uuid(self) -> None:
        packet = await read_audiosocket_packet(self.reader)
        if packet is None:
            self.closed.set()
            return
        kind, payload = packet
        if kind == AUDIOSOCKET_KIND_UUID:
            self.call_uuid = audiosocket_uuid(payload)
            return
        LOGGER.debug("AudioSocket first packet was not UUID kind=0x%02x len=%d", kind, len(payload))
        if kind == AUDIOSOCKET_KIND_HANGUP:
            self.closed.set()

    def _load_target_language_for_uuid(self) -> None:
        target_dir = Path(os.getenv("TRANSLATION_TARGET_DIR", "/var/log/asterisk/ai/translation_targets"))
        target_file = target_dir / f"{self.call_uuid}.json"
        if not target_file.exists():
            return
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
        except Exception:
            LOGGER.exception("Could not read translation target file %s", target_file)
            return
        self.target_language = normalize_translation_language(
            data.get("target_language"),
            default=self.target_language,
        )

    async def _asterisk_to_openai(self, ws: Any) -> None:
        try:
            while not self.closed.is_set():
                packet = await read_audiosocket_packet(self.reader)
                if packet is None:
                    break
                kind, payload = packet
                if kind == AUDIOSOCKET_KIND_UUID:
                    self.call_uuid = audiosocket_uuid(payload)
                    self._load_target_language_for_uuid()
                    LOGGER.info("AudioSocket call uuid=%s target=%s", self.call_uuid, self.target_language)
                elif kind == AUDIOSOCKET_KIND_AUDIO_8K:
                    if not payload:
                        continue
                    self.audio_packets_in += 1
                    self.audio_bytes_in += len(payload)
                    if self.audio_packets_in == 1 or self.audio_packets_in % 50 == 0:
                        LOGGER.info(
                            "AudioSocket audio in uuid=%s packets=%d bytes=%d",
                            self.call_uuid,
                            self.audio_packets_in,
                            self.audio_bytes_in,
                        )
                    pcm24k = self.input_resampler.process(payload)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.input_audio_buffer.append",
                                "audio": base64.b64encode(pcm24k).decode("ascii"),
                            }
                        )
                    )
                elif kind == AUDIOSOCKET_KIND_DTMF:
                    LOGGER.debug("AudioSocket DTMF uuid=%s payload=%r", self.call_uuid, payload)
                elif kind == AUDIOSOCKET_KIND_HANGUP:
                    LOGGER.info("AudioSocket hangup uuid=%s", self.call_uuid)
                    break
                elif kind == AUDIOSOCKET_KIND_ERROR:
                    LOGGER.warning("AudioSocket error uuid=%s payload=%r", self.call_uuid, payload)
                    break
                else:
                    LOGGER.debug("AudioSocket ignored packet kind=0x%02x len=%d", kind, len(payload))
        except Exception:
            LOGGER.exception("AudioSocket receive loop failed uuid=%s", self.call_uuid)
        finally:
            self.closed.set()
            LOGGER.info(
                "AudioSocket receive loop closed uuid=%s packets=%d bytes=%d",
                self.call_uuid,
                self.audio_packets_in,
                self.audio_bytes_in,
            )
            with contextlib.suppress(Exception):
                await ws.close()

    async def _openai_to_asterisk(self, ws: Any) -> None:
        try:
            while not self.closed.is_set():
                raw = await ws.recv()
                event = json.loads(raw)
                event_type = event.get("type", "")
                if event_type in {"session.output_audio.delta", "translation.audio.delta", "response.audio.delta", "response.output_audio.delta"}:
                    pcm24k = base64.b64decode(event.get("delta", ""))
                    if not pcm24k:
                        continue
                    pcm8k = self.output_resampler.process(pcm24k)
                    self.audio_deltas_out += 1
                    self.audio_bytes_out += len(pcm8k)
                    if self.audio_deltas_out == 1 or self.audio_deltas_out % 20 == 0:
                        LOGGER.info(
                            "OpenAI translated audio out uuid=%s deltas=%d bytes=%d",
                            self.call_uuid,
                            self.audio_deltas_out,
                            self.audio_bytes_out,
                        )
                    await self._write_pcm_to_asterisk(pcm8k)
                elif event_type in {
                    "session.output_audio_transcript.delta",
                    "session.output_text.delta",
                    "session.output_transcript.delta",
                    "translation.text.delta",
                    "translation.transcript.delta",
                }:
                    LOGGER.debug("Translation text uuid=%s delta=%s", self.call_uuid, event.get("delta", ""))
                elif event_type == "error":
                    LOGGER.error("OpenAI translation error uuid=%s error=%s", self.call_uuid, event.get("error", event))
                elif event_type in {"session.created", "session.updated"}:
                    LOGGER.info("OpenAI translation event uuid=%s type=%s", self.call_uuid, event_type)
                else:
                    LOGGER.debug("OpenAI translation event uuid=%s type=%s", self.call_uuid, event_type)
        except Exception:
            if not self.closed.is_set():
                LOGGER.exception("OpenAI receive loop failed uuid=%s", self.call_uuid)
        finally:
            self.closed.set()
            LOGGER.info(
                "OpenAI receive loop closed uuid=%s deltas=%d bytes=%d",
                self.call_uuid,
                self.audio_deltas_out,
                self.audio_bytes_out,
            )

    async def _write_pcm_to_asterisk(self, pcm8k: bytes) -> None:
        frame_bytes = max(2, self.output_frame_bytes - (self.output_frame_bytes % 2))
        frame_seconds = frame_bytes / (self.output_rate * 2)
        async with self.write_lock:
            for offset in range(0, len(pcm8k), frame_bytes):
                chunk = pcm8k[offset : offset + frame_bytes]
                if not chunk:
                    continue
                await write_audiosocket_packet(self.writer, AUDIOSOCKET_KIND_AUDIO_8K, chunk)
                if self.output_pacing_enabled:
                    await asyncio.sleep(frame_seconds)

    async def close(self) -> None:
        self.closed.set()
        with contextlib.suppress(Exception):
            await write_audiosocket_packet(self.writer, AUDIOSOCKET_KIND_HANGUP)
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    session = AudioSocketTranslationSession(reader, writer)
    try:
        await session.run()
    finally:
        await session.close()


async def main_async() -> None:
    host = os.getenv("TRANSLATION_AUDIOSOCKET_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("TRANSLATION_AUDIOSOCKET_PORT", "9019"))
    server = await asyncio.start_server(handle_connection, host, port)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOGGER.info("AudioSocket translation server listening on %s", sockets)

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
        LOGGER.exception("Fatal AudioSocket translation server failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
