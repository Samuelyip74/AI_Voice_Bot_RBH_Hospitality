#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from audio_utils import (
    asterisk_playback_wav,
    capture_utterance_from_fd3,
    drain_audio_fd,
    openai_input_pcm,
    pcm_duration_seconds,
    write_wav,
)
from openai_realtime_translation_client import (
    OpenAIRealtimeTranslationClient,
    normalize_translation_language,
)
from voice_assistant_eagi import (
    agi_answer,
    agi_command,
    agi_get_variable,
    agi_response_is_dead_channel,
    agi_stream_file,
    agi_variable_value,
    agi_verbose,
    parse_agi_env,
    synthesize_text_phrase,
)


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(stream=sys.stderr, level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("translation_bridge_eagi")


async def run_translation_bridge() -> int:
    agi_env = parse_agi_env()
    call_id = agi_env.get("agi_uniqueid", "translation").replace("/", "_")
    sounds_dir = Path(os.getenv("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/ai"))
    sounds_dir.mkdir(parents=True, exist_ok=True)

    source_codec = os.getenv("ASTERISK_EAGI_CODEC", "slin")
    source_rate = int(os.getenv("ASTERISK_EAGI_SAMPLE_RATE", "8000"))
    channel_target_language = agi_variable_value(agi_get_variable("TRANSLATION_TARGET_LANGUAGE"))
    target_language = normalize_translation_language(
        channel_target_language or os.getenv("TRANSLATION_TARGET_LANGUAGE", os.getenv("TRANSLATION_DEFAULT_TARGET_LANGUAGE", "en"))
    )
    silence_timeout_ms = int(os.getenv("TRANSLATION_SILENCE_TIMEOUT_MS", os.getenv("SILENCE_TIMEOUT_MS", "650")))
    max_utterance_seconds = int(os.getenv("TRANSLATION_MAX_UTTERANCE_SECONDS", "12"))
    max_turns = int(os.getenv("TRANSLATION_MAX_TURNS", "80"))
    record_audio = os.getenv("RECORD_AUDIO", "false").lower() == "true"
    eagi_frame_bytes = int(source_rate * 20 / 1000) * (2 if source_codec.lower() in {"slin", "slin16", "pcm16"} else 1)
    drain_after_playback_ms = int(os.getenv("EAGI_DRAIN_AFTER_PLAYBACK_MS", "250"))

    agi_answer()
    agi_verbose(f"AI translation bridge started id={call_id} target={target_language}", 1)
    client = OpenAIRealtimeTranslationClient()

    start_phrase = os.getenv(
        "TRANSLATION_START_TEXT",
        "Live translation is starting now. Please speak one sentence at a time.",
    )
    try:
        from call_session import CallSession
        from openai_realtime_client import OpenAIRealtimeClient

        tts_client = OpenAIRealtimeClient()
        tts_session = CallSession(call_id=f"{call_id}_translation", preferred_language=target_language)
        start_wav = await synthesize_text_phrase(tts_client, tts_session, start_phrase, sounds_dir / f"{call_id}_translation_start.wav")
        response = agi_stream_file(str(start_wav.with_suffix("")))
        if agi_response_is_dead_channel(response):
            return 0
        drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)
    except Exception:
        LOGGER.exception("Could not synthesize translation start phrase")

    for turn in range(1, max_turns + 1):
        pcm = capture_utterance_from_fd3(
            fd=3,
            codec=source_codec,
            sample_rate=source_rate,
            silence_timeout_ms=silence_timeout_ms,
            max_seconds=max_utterance_seconds,
            no_speech_timeout_seconds=float(os.getenv("TRANSLATION_NO_SPEECH_TIMEOUT_SECONDS", "8")),
        )
        if pcm_duration_seconds(pcm, source_rate) < float(os.getenv("TRANSLATION_MIN_AUDIO_SECONDS", "0.35")):
            continue
        if record_audio:
            write_wav(sounds_dir / f"{call_id}_translation_{turn}_source.wav", pcm, source_rate)

        try:
            result = await client.translate_utterance(openai_input_pcm(pcm, source_rate), target_language)
        except Exception as exc:
            LOGGER.exception("Realtime translation failed")
            agi_verbose(f"AI translation failed: {exc}", 1)
            continue

        if result.error:
            agi_verbose(f"AI translation endpoint error: {result.error}", 1)
            continue
        if not result.translated_audio_pcm24k:
            agi_verbose("AI translation returned no audio", 1)
            continue

        translated_wav = asterisk_playback_wav(
            sounds_dir / f"{call_id}_translation_{turn}.wav",
            result.translated_audio_pcm24k,
            output_rate=source_rate,
        )
        playback_response = agi_stream_file(str(translated_wav.with_suffix("")))
        if agi_response_is_dead_channel(playback_response):
            break
        drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)

    return 0


def main() -> int:
    try:
        return asyncio.run(run_translation_bridge())
    except Exception as exc:
        LOGGER.exception("Fatal translation bridge failure")
        try:
            agi_verbose(f"Fatal translation bridge failure: {exc}", 1)
        except Exception:
            pass
        return 1
    finally:
        try:
            agi_command("HANGUP")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
