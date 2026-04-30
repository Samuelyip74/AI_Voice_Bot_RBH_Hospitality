#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
import sys
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

from audio_utils import asterisk_playback_wav, capture_utterance_from_fd3, openai_input_pcm, write_wav
from call_session import CallSession, detect_language_from_text, determine_transfer_action, should_transfer_deterministic
from openai_realtime_client import OpenAIRealtimeClient


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(stream=sys.stderr, level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("voice_assistant_eagi")


TRANSFER_PHRASES = {
    "en": "Of course. I'll connect you to our concierge team now. Please hold for a moment.",
    "zh": "我现在为您转接给同事。",
    "zh-yue": "我而家幫你轉接俾同事。",
    "ms": "Saya akan sambungkan anda kepada rakan sekerja sekarang.",
    "ta": "இப்போது உங்களை ஒரு சக பணியாளரிடம் மாற்றுகிறேன்.",
    "ja": "これから担当者におつなぎします。",
    "ko": "지금 담당자에게 연결해 드리겠습니다.",
    "th": "ฉันจะโอนสายให้เจ้าหน้าที่ตอนนี้",
    "vi": "Tôi sẽ chuyển bạn đến một đồng nghiệp ngay bây giờ.",
    "id": "Saya akan menghubungkan Anda ke rekan saya sekarang.",
}

ROOM_SERVICE_TRANSFER_PHRASES = {
    "en": "Of course. I'll connect you to our in-room dining team now. Please hold for a moment.",
    "zh": "当然可以。我现在为您转接到客房送餐团队，请稍等。",
    "ms": "Sudah tentu. Saya akan sambungkan anda kepada pasukan hidangan dalam bilik sekarang. Sila tunggu sebentar.",
    "ta": "நிச்சயமாக. உங்களை இப்போது அறை உணவு சேவை குழுவுடன் இணைக்கிறேன். தயவுசெய்து சிறிது காத்திருக்கவும்.",
    "ja": "かしこまりました。ただいまインルームダイニング担当へおつなぎします。少々お待ちください。",
    "ko": "물론입니다. 지금 객실 다이닝 팀으로 연결해 드리겠습니다. 잠시만 기다려 주세요.",
    "th": "ได้ค่ะ ฉันจะโอนสายไปยังทีมบริการอาหารในห้องพัก กรุณาถือสายรอสักครู่",
    "vi": "Dạ được. Tôi sẽ chuyển quý khách đến đội phục vụ ăn uống tại phòng ngay bây giờ. Xin vui lòng chờ trong giây lát.",
    "id": "Tentu. Saya akan menghubungkan Anda ke tim in-room dining sekarang. Mohon tunggu sebentar.",
}

UNAVAILABLE_PHRASES = {
    "en": "I'm sorry, my colleague is not available right now.",
    "zh": "抱歉，我的同事现在无法接听。",
    "zh-yue": "唔好意思，我同事而家未能接聽。",
    "ms": "Maaf, rakan sekerja saya tidak tersedia sekarang.",
    "ta": "மன்னிக்கவும், என் சக பணியாளர் இப்போது கிடைக்கவில்லை.",
    "ja": "申し訳ありません。担当者はただいま対応できません。",
    "ko": "죄송합니다. 지금은 담당자가 받을 수 없습니다.",
    "th": "ขออภัย เจ้าหน้าที่ไม่พร้อมรับสายในตอนนี้",
    "vi": "Xin lỗi, đồng nghiệp của tôi hiện không thể nghe máy.",
    "id": "Maaf, rekan saya belum tersedia saat ini.",
}


DEFAULT_GREETING_TEXT = "Hello, this is the AI assistant. How can I help you today?"


def parse_agi_env(stdin: object = sys.stdin) -> dict[str, str]:
    env: dict[str, str] = {}
    while True:
        line = stdin.readline()
        if line == "":
            break
        line = line.strip()
        if not line:
            break
        if ":" in line:
            key, value = line.split(":", 1)
            env[key.strip()] = value.strip()
    return env


def agi_command(command: str) -> str:
    print(command, flush=True)
    response = sys.stdin.readline().strip()
    LOGGER.debug("AGI %s -> %s", command, response)
    return response


def agi_answer() -> str:
    return agi_command("ANSWER")


def agi_stream_file(path_without_extension: str) -> str:
    return agi_command(f'STREAM FILE "{path_without_extension}" ""')


def agi_exec(app: str, args: str = "") -> str:
    return agi_command(f"EXEC {app} {args}".strip())


def agi_hangup() -> str:
    return agi_command("HANGUP")


def agi_get_variable(name: str) -> str:
    return agi_command(f"GET VARIABLE {name}")


def agi_verbose(message: str, level: int = 1) -> str:
    safe = message.replace('"', "'")
    return agi_command(f'VERBOSE "{safe}" {level}')


def create_start_tone(sounds_dir: Path, call_id: str) -> Path:
    """Create a short local tone so callers know the EAGI app is listening."""
    sample_rate = 8000
    pcm = bytearray()
    for i in range(int(sample_rate * 0.22)):
        sample = int(9000 * math.sin(2 * math.pi * 880 * i / sample_rate))
        pcm.extend(struct.pack("<h", sample))
    for _ in range(int(sample_rate * 0.08)):
        pcm.extend(struct.pack("<h", 0))
    for i in range(int(sample_rate * 0.22)):
        sample = int(9000 * math.sin(2 * math.pi * 660 * i / sample_rate))
        pcm.extend(struct.pack("<h", sample))
    return write_wav(sounds_dir / f"{call_id}_ready.wav", bytes(pcm), sample_rate)


def transfer_target_for_extension(extension: str) -> str:
    template = os.getenv("TRANSFER_TARGET_TEMPLATE", "sip:{extension}@313.apac1.sip.openrainbow.com")
    return template.format(extension=extension)


def normalize_transfer_extension(extension: str, transfer_type: str, human_extension: str, room_service_extension: str) -> str:
    value = (extension or "").strip().lower().replace("-", "_").replace(" ", "_")
    room_service_aliases = {"1921", "room_service", "in_room_dining", "in-room_dining", "dining", "restaurant"}
    human_aliases = {
        "1920",
        "front_desk",
        "frontdesk",
        "reception",
        "receptionist",
        "concierge",
        "concierge_team",
        "operator",
        "human",
        "agent",
        "manager",
    }
    if transfer_type == "room_service" or value in room_service_aliases:
        return room_service_extension
    if transfer_type == "human" or value in human_aliases:
        return human_extension
    return extension if extension.isdigit() else human_extension


async def synthesize_text_phrase(
    client: OpenAIRealtimeClient,
    session: CallSession,
    text: str,
    target_wav: Path,
) -> Path:
    """Use Realtime as a short TTS turn for prompts that are not based on caller audio."""
    from openai_realtime_client import REALTIME_URL
    import base64
    import json
    import websockets

    url = f"{REALTIME_URL}?model={client.model}"
    headers = {"Authorization": f"Bearer {client.api_key}"}
    pcm24k = b""
    async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
        await client._send_session_update(ws, session)
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"Say exactly this sentence: {text}"}],
                    },
                }
            )
        )
        await ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"]}}))
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") in {"response.audio.delta", "response.output_audio.delta"}:
                pcm24k += base64.b64decode(event.get("delta", ""))
            elif event.get("type") == "response.done":
                break
            elif event.get("type") == "error":
                raise RuntimeError(f"OpenAI phrase synthesis failed: {event.get('error', event)}")

    return asterisk_playback_wav(target_wav, pcm24k)


async def synthesize_transfer_phrase(client: OpenAIRealtimeClient, session: CallSession, text: str, sounds_dir: Path, turn: int) -> Path:
    return await synthesize_text_phrase(client, session, text, sounds_dir / f"{session.call_id}_{turn}_transfer.wav")


async def run_call() -> int:
    agi_env = parse_agi_env()
    default_language = os.getenv("DEFAULT_LANGUAGE", "en")
    session = CallSession.from_agi_env(agi_env, default_language)
    sounds_dir = Path(os.getenv("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/ai"))
    sounds_dir.mkdir(parents=True, exist_ok=True)
    record_audio = os.getenv("RECORD_AUDIO", "false").lower() == "true"
    silence_timeout_ms = int(os.getenv("SILENCE_TIMEOUT_MS", "900"))
    max_utterance_seconds = int(os.getenv("MAX_UTTERANCE_SECONDS", "15"))
    transfer_extension = os.getenv("TRANSFER_EXTENSION", os.getenv("HUMAN_TRANSFER_EXTENSION", "1920"))
    room_service_transfer_extension = os.getenv("ROOM_SERVICE_TRANSFER_EXTENSION", "1921")
    source_codec = os.getenv("ASTERISK_EAGI_CODEC", "slin")
    source_rate = int(os.getenv("ASTERISK_EAGI_SAMPLE_RATE", "8000"))
    enable_ready_tone = os.getenv("ENABLE_READY_TONE", "false").lower() == "true"
    enable_ai_greeting = os.getenv("ENABLE_AI_GREETING", "true").lower() == "true"
    greeting_text = os.getenv("AI_GREETING_TEXT", DEFAULT_GREETING_TEXT)
    no_audio_max_turns = int(os.getenv("NO_AUDIO_MAX_TURNS", "20"))

    agi_verbose(f"AI assistant call started id={session.call_id}", 1)
    agi_answer()
    session.append_event("call_started", {"caller_id": session.caller_id, "codec": source_codec, "sample_rate": source_rate})

    if enable_ready_tone:
        try:
            start_tone = create_start_tone(sounds_dir, session.call_id)
            response = agi_stream_file(str(start_tone.with_suffix("")))
            session.append_event("start_tone_played", {"file": str(start_tone), "agi_response": response})
        except Exception as exc:
            LOGGER.exception("Could not play start tone")
            session.append_event("start_tone_error", {"error": str(exc)})

    client = OpenAIRealtimeClient(
        transfer_extension=transfer_extension,
        room_service_extension=room_service_transfer_extension,
    )

    if enable_ai_greeting:
        try:
            agi_verbose("AI assistant generating greeting", 1)
            greeting_wav = await synthesize_text_phrase(client, session, greeting_text, sounds_dir / f"{session.call_id}_greeting.wav")
            greeting_response = agi_stream_file(str(greeting_wav.with_suffix("")))
            session.append_event("greeting_played", {"file": str(greeting_wav), "text": greeting_text, "agi_response": greeting_response})
        except Exception as exc:
            LOGGER.exception("Could not synthesize or play AI greeting")
            session.append_event("greeting_error", {"error": str(exc)})

    no_audio_turns = 0

    for turn in range(1, 50):
        try:
            agi_verbose(f"AI assistant listening turn={turn}", 1)
            pcm = capture_utterance_from_fd3(
                fd=3,
                codec=source_codec,
                sample_rate=source_rate,
                silence_timeout_ms=silence_timeout_ms,
                max_seconds=max_utterance_seconds,
            )
            session.append_event("audio_captured", {"turn": turn, "bytes": len(pcm)})
            if len(pcm) < source_rate * 2 * 0.25:
                agi_verbose(f"AI assistant ignored short audio bytes={len(pcm)}", 1)
                if len(pcm) == 0:
                    no_audio_turns += 1
                    session.append_event(
                        "no_eagi_audio",
                        {
                            "turn": turn,
                            "count": no_audio_turns,
                            "hint": "No bytes arrived on EAGI fd 3. Check caller microphone, inbound RTP, NAT/firewall, and RTP debug.",
                        },
                    )
                    if no_audio_turns >= no_audio_max_turns:
                        break
                    continue
                continue
            no_audio_turns = 0

            if record_audio:
                write_wav(sounds_dir / f"{session.call_id}_{turn}_caller.wav", pcm, source_rate)

            agi_verbose(f"AI assistant sending turn={turn} to OpenAI bytes={len(pcm)}", 1)
            pcm24k = openai_input_pcm(pcm, source_rate)
            result = await client.process_turn(session, pcm24k)
            language, confidence = detect_language_from_text(result.transcript, session.preferred_language)
            session.update_language(language, confidence, source="post_response_transcript")
            session.append_event("user", {"role": "user", "text": result.transcript, "language": session.preferred_language})
            session.append_event("assistant", {"role": "assistant", "text": result.response_text})
            if result.error:
                session.append_event("openai_response_error", {"turn": turn, "error": result.error})

            deterministic_action = determine_transfer_action(
                result.transcript,
                failed_intent_count=session.failed_intent_count,
                human_extension=transfer_extension,
                room_service_extension=room_service_transfer_extension,
            )
            if deterministic_action and result.transfer_action is None:
                result.transfer_action = deterministic_action

            transfer, reason = should_transfer_deterministic(result.transcript, session.failed_intent_count)
            target_extension = transfer_extension
            transfer_type = "human"
            if result.transfer_action:
                transfer = True
                reason = result.transfer_action.get("reason", "model requested transfer")
                target_extension = result.transfer_action.get("extension", transfer_extension)
                transfer_type = result.transfer_action.get("transfer_type") or (
                    "room_service" if target_extension == room_service_transfer_extension else "human"
                )
                target_extension = normalize_transfer_extension(
                    target_extension,
                    transfer_type,
                    transfer_extension,
                    room_service_transfer_extension,
                )
                session.append_event(
                    "transfer_action_detected",
                    {
                        "extension": target_extension,
                        "transfer_type": transfer_type,
                        "reason": reason,
                    },
                )

            if result.response_audio_pcm24k:
                response_wav = asterisk_playback_wav(sounds_dir / f"{session.call_id}_{turn}.wav", result.response_audio_pcm24k)
                playback_response = agi_stream_file(str(response_wav.with_suffix("")))
                session.append_event("assistant_audio_played", {"turn": turn, "file": str(response_wav), "agi_response": playback_response})
                time.sleep(0.25)
            else:
                session.append_event("assistant_audio_missing", {"turn": turn, "text": result.response_text})
                if not transfer:
                    continue

            if transfer:
                session.request_transfer(reason or "transfer requested")
                phrase_map = ROOM_SERVICE_TRANSFER_PHRASES if transfer_type == "room_service" else TRANSFER_PHRASES
                phrase = phrase_map.get(session.preferred_language, phrase_map["en"])
                try:
                    transfer_wav = await synthesize_transfer_phrase(client, session, phrase, sounds_dir, turn)
                    agi_stream_file(str(transfer_wav.with_suffix("")))
                except Exception:
                    LOGGER.exception("Could not synthesize transfer phrase")
                transfer_target = transfer_target_for_extension(target_extension)
                transfer_response = agi_exec("Transfer", transfer_target)
                transfer_status = agi_get_variable("TRANSFERSTATUS")
                transfer_status_protocol = agi_get_variable("TRANSFERSTATUSPROTOCOL")
                session.append_event(
                    "transfer_result",
                    {
                        "extension": target_extension,
                        "target": transfer_target,
                        "transfer_type": transfer_type,
                        "response": transfer_response,
                        "status": transfer_status,
                        "status_protocol": transfer_status_protocol,
                    },
                )
                transfer_failed = any(
                    marker in f"{transfer_response} {transfer_status} {transfer_status_protocol}".upper()
                    for marker in ("RESULT=-1", "UNSUPPORTED", "FAILURE")
                )
                if transfer_failed:
                    unavailable = UNAVAILABLE_PHRASES.get(session.preferred_language, UNAVAILABLE_PHRASES["en"])
                    session.append_event("transfer_unavailable", {"message": unavailable})
                    continue
                break

        except (BrokenPipeError, KeyboardInterrupt):
            session.append_event("hangup", {"reason": "caller disconnected"})
            break
        except Exception as exc:
            LOGGER.error("Turn failed: %s\n%s", exc, traceback.format_exc())
            session.append_event("error", {"error": str(exc)})
            break

    agi_hangup()
    return 0


def main() -> int:
    try:
        return asyncio.run(run_call())
    except Exception as exc:
        LOGGER.error("Fatal EAGI failure: %s\n%s", exc, traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
