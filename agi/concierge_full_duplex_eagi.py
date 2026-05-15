#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from call_session import CallSession
from audio_utils import drain_audio_fd
from voice_assistant_eagi import (
    build_greeting_text,
    CLOSING_PHRASES,
    ROOM_SERVICE_TRANSFER_PHRASES,
    ROOM_TRANSFER_PHRASES,
    TRANSFER_PHRASES,
    agi_answer,
    agi_exec,
    agi_hangup,
    agi_response_is_dead_channel,
    agi_set_variable,
    agi_stream_file,
    agi_variable_value,
    agi_get_variable,
    enrich_session_caller_details,
    finalize_transcript_email,
    normalize_transfer_extension,
    notify_rainbow_transfer_transcript,
    parse_agi_env,
    synthesize_text_phrase,
    synthesize_transfer_phrase,
    synthesize_cached_greeting,
    transfer_target_for_extension,
)
from openai_realtime_client import OpenAIRealtimeClient


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(stream=sys.stderr, level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("concierge_full_duplex_eagi")


def session_dir() -> Path:
    return Path(os.getenv("CONCIERGE_SESSION_DIR", "/var/log/asterisk/ai/concierge_sessions"))


def action_dir() -> Path:
    return Path(os.getenv("CONCIERGE_ACTION_DIR", "/var/log/asterisk/ai/concierge_actions"))


def write_session_file(audiosocket_id: str, session: CallSession) -> Path:
    session_dir().mkdir(parents=True, exist_ok=True)
    path = session_dir() / f"{audiosocket_id}.json"
    path.write_text(
        json.dumps(
            {
                "audiosocket_uuid": audiosocket_id,
                "call_id": session.call_id,
                "caller_id": session.caller_id,
                "caller_name": session.caller_name,
                "room_number": session.room_number,
                "sip_from_header": session.sip_from_header,
                "preferred_language": session.preferred_language,
                "created_at": time.time(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def refresh_session_history_from_log(session: CallSession) -> None:
    log_path = session.log_dir / f"{session.call_id}.jsonl"
    if not log_path.exists():
        return
    events = []
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    if not events:
        return
    session.history = events
    for event in events:
        if event.get("type") == "language_change" and event.get("language"):
            session.preferred_language = str(event.get("language"))
        elif event.get("type") == "user" and event.get("language"):
            session.preferred_language = str(event.get("language"))


async def play_closing_phrase(session: CallSession) -> None:
    closing = CLOSING_PHRASES.get(session.preferred_language, CLOSING_PHRASES["en"])
    try:
        sounds_dir = Path(os.getenv("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/ai"))
        client = OpenAIRealtimeClient()
        wav = await synthesize_text_phrase(client, session, closing, sounds_dir / f"{session.call_id}_full_duplex_closing.wav")
        response = agi_stream_file(str(wav.with_suffix("")))
        session.append_event("closing_phrase_played", {"file": str(wav), "text": closing, "agi_response": response})
        if agi_response_is_dead_channel(response):
            session.append_event("hangup", {"reason": "dead channel during full-duplex closing phrase playback"})
            return
        source_codec = os.getenv("ASTERISK_EAGI_CODEC", "slin")
        source_rate = int(os.getenv("ASTERISK_EAGI_SAMPLE_RATE", "8000"))
        eagi_frame_bytes = int(source_rate * 20 / 1000) * (2 if source_codec.lower() in {"slin", "slin16", "pcm16"} else 1)
        drained = drain_audio_fd(3, eagi_frame_bytes, int(os.getenv("EAGI_DRAIN_AFTER_PLAYBACK_MS", "250")))
        if drained:
            session.append_event("full_duplex_closing_audio_drained", {"bytes": drained})
    except Exception as exc:
        LOGGER.exception("Could not play full-duplex closing phrase")
        session.append_event("closing_phrase_error", {"error": str(exc)})


async def handle_action(session: CallSession, audiosocket_id: str) -> None:
    path = action_dir() / f"{audiosocket_id}.json"
    if not path.exists():
        return
    action = json.loads(path.read_text(encoding="utf-8"))
    session.append_event("full_duplex_action_loaded", {"audiosocket_uuid": audiosocket_id, "action": action})
    if action.get("action") == "end_call":
        session.append_event(
            "call_closing",
            {"reason": action.get("reason", "guest indicated there are no more requests")},
        )
        await play_closing_phrase(session)
        agi_hangup()
        return
    if action.get("action") != "transfer":
        return
    transfer_extension = os.getenv("TRANSFER_EXTENSION", os.getenv("HUMAN_TRANSFER_EXTENSION", "1920"))
    room_service_extension = os.getenv("ROOM_SERVICE_TRANSFER_EXTENSION", "1921")
    transfer_type = action.get("transfer_type", "human")
    target_extension = normalize_transfer_extension(str(action.get("extension") or transfer_extension), transfer_type, transfer_extension, room_service_extension)
    if transfer_type == "room_service":
        phrase_map = ROOM_SERVICE_TRANSFER_PHRASES
    elif transfer_type == "room":
        phrase_map = ROOM_TRANSFER_PHRASES
    else:
        phrase_map = TRANSFER_PHRASES
    phrase = phrase_map.get(session.preferred_language, phrase_map["en"])
    try:
        sounds_dir = Path(os.getenv("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/ai"))
        client = OpenAIRealtimeClient()
        wav = await synthesize_transfer_phrase(client, session, phrase, sounds_dir, 0)
        agi_stream_file(str(wav.with_suffix("")))
    except Exception:
        LOGGER.exception("Could not play full-duplex transfer phrase")
    try:
        refresh_session_history_from_log(session)
        rainbow_transfer_result = await asyncio.to_thread(
            notify_rainbow_transfer_transcript,
            session,
            transfer_type,
            target_extension,
        )
        session.append_event("transfer_transcript_rainbow_result", rainbow_transfer_result)
    except Exception as exc:
        LOGGER.exception("Could not send full-duplex transfer transcript to Rainbow")
        session.append_event(
            "transfer_transcript_rainbow_error",
            {"error": str(exc), "transfer_type": transfer_type, "extension": target_extension},
        )
    agi_exec("Transfer", transfer_target_for_extension(target_extension))


async def run_call() -> int:
    session: CallSession | None = None
    agi_env = parse_agi_env()
    session = CallSession.from_agi_env(agi_env, os.getenv("DEFAULT_LANGUAGE", "en"))
    try:
        agi_answer()
        enrich_session_caller_details(session)
        sounds_dir = Path(os.getenv("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/ai"))
        if os.getenv("CONCIERGE_BOOTSTRAP_GREETING_ENABLED", "true").lower() == "true":
            try:
                greeting_text, greeting_personalized = build_greeting_text(session)
                client = OpenAIRealtimeClient()
                greeting_wav = await synthesize_cached_greeting(client, session, greeting_text, sounds_dir)
                response = agi_stream_file(str(greeting_wav.with_suffix("")))
                session.append_event(
                    "full_duplex_bootstrap_greeting_played",
                    {
                        "file": str(greeting_wav),
                        "text": greeting_text,
                        "personalized": greeting_personalized,
                        "agi_response": response,
                    },
                )
                source_codec = os.getenv("ASTERISK_EAGI_CODEC", "slin")
                source_rate = int(os.getenv("ASTERISK_EAGI_SAMPLE_RATE", "8000"))
                eagi_frame_bytes = int(source_rate * 20 / 1000) * (2 if source_codec.lower() in {"slin", "slin16", "pcm16"} else 1)
                drained = drain_audio_fd(3, eagi_frame_bytes, int(os.getenv("EAGI_DRAIN_AFTER_PLAYBACK_MS", "250")))
                if drained:
                    session.append_event("full_duplex_bootstrap_audio_drained", {"bytes": drained})
            except Exception as exc:
                LOGGER.exception("Could not play full-duplex bootstrap greeting")
                session.append_event("full_duplex_bootstrap_greeting_error", {"error": str(exc)})
        audiosocket_id = str(uuid.uuid4())
        session_file = write_session_file(audiosocket_id, session)
        session.append_event("full_duplex_bootstrap", {"audiosocket_uuid": audiosocket_id, "session_file": str(session_file)})
        agi_set_variable("AUDIOSOCKET_UUID", audiosocket_id)
        service = os.getenv("CONCIERGE_AUDIOSOCKET_SERVICE", "127.0.0.1:9020")
        response = agi_exec("AudioSocket", f"{audiosocket_id},{service}")
        session.append_event("full_duplex_audiosocket_result", {"audiosocket_uuid": audiosocket_id, "service": service, "response": response})
        refresh_session_history_from_log(session)
        await handle_action(session, audiosocket_id)
        return 0
    finally:
        if session is not None:
            refresh_session_history_from_log(session)
            finalize_transcript_email(session)


def main() -> int:
    try:
        return asyncio.run(run_call())
    except Exception as exc:
        LOGGER.exception("Fatal full-duplex concierge failure")
        try:
            agi_hangup()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
