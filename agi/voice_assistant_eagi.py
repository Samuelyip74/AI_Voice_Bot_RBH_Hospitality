#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import signal
import struct
import sys
import time
import traceback
import json
import smtplib
import subprocess
import threading
from pathlib import Path
from email.message import EmailMessage
from email.utils import formataddr
from urllib import request
from urllib.parse import unquote

from dotenv import load_dotenv

from audio_utils import (
    asterisk_playback_wav,
    capture_utterance_from_fd3,
    drain_audio_fd,
    openai_input_pcm,
    pcm_duration_seconds,
    rms_dbfs,
    speech_ratio,
    write_wav,
)
from call_session import (
    CallSession,
    detect_language_from_text,
    determine_transfer_action,
    should_end_call_deterministic,
    should_transfer_deterministic,
)
from openai_realtime_client import OpenAIRealtimeClient


load_dotenv("/var/lib/asterisk/agi-bin/.env", override=True)
load_dotenv(override=True)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(stream=sys.stderr, level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("voice_assistant_eagi")

CURRENT_SESSION: CallSession | None = None
TRANSCRIPT_EMAIL_SENT = False


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

ROOM_TRANSFER_PHRASES = {
    "en": "Of course. I'll connect you to the room now. Please hold for a moment.",
    "zh": "当然可以。我现在为您转接到房间，请稍等。",
    "zh-yue": "當然可以。我而家幫你轉接去房間，請稍等。",
    "ms": "Baik, saya akan sambungkan anda ke bilik itu sekarang. Sila tunggu sebentar.",
    "ta": "நிச்சயமாக. இப்போது உங்களை அந்த அறைக்கு இணைக்கிறேன். தயவுசெய்து காத்திருக்கவும்.",
    "ja": "かしこまりました。ただいまお部屋へおつなぎします。少々お待ちください。",
    "ko": "물론입니다. 지금 객실로 연결해 드리겠습니다. 잠시만 기다려 주세요.",
    "th": "ได้ค่ะ ฉันจะโอนสายไปยังห้องพักนั้น กรุณาถือสายรอสักครู่",
    "vi": "Dạ được. Tôi sẽ chuyển quý khách đến phòng đó ngay bây giờ. Xin vui lòng chờ trong giây lát.",
    "id": "Tentu. Saya akan menghubungkan Anda ke kamar tersebut sekarang. Mohon tunggu sebentar.",
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

CLOSING_PHRASES = {
    "en": "You're very welcome. Thank you for calling, and have a pleasant day.",
    "zh": "不客气。感谢您的来电，祝您今天愉快。",
    "zh-yue": "唔使客氣。多謝你致電，祝你有愉快嘅一日。",
    "ms": "Sama-sama. Terima kasih kerana menghubungi kami, semoga hari anda menyenangkan.",
    "ta": "மிகவும் மகிழ்ச்சி. அழைத்ததற்கு நன்றி, இனிய நாள் அமையட்டும்.",
    "ja": "どういたしまして。お電話ありがとうございました。どうぞ良い一日をお過ごしください。",
    "ko": "천만에요. 전화해 주셔서 감사합니다. 좋은 하루 보내세요.",
    "th": "ยินดีค่ะ ขอบคุณที่โทรมา ขอให้มีวันที่ดีนะคะ",
    "vi": "Rất hân hạnh. Cảm ơn quý khách đã gọi, chúc quý khách một ngày tốt lành.",
    "id": "Sama-sama. Terima kasih telah menghubungi kami, semoga hari Anda menyenangkan.",
}

SERVICE_REQUEST_SUBMITTED_PHRASES = {
    "en": "Thank you. I have submitted your request to our hotel team. Is there anything else I can help with?",
    "zh": "谢谢。我已经把您的请求提交给酒店团队。还有其他可以帮您的吗？",
    "zh-yue": "多謝。我已經幫你將要求提交俾酒店團隊。仲有冇其他可以幫你？",
    "ms": "Terima kasih. Saya telah menghantar permintaan anda kepada pasukan hotel. Ada apa-apa lagi yang boleh saya bantu?",
    "id": "Terima kasih. Saya telah mengirim permintaan Anda kepada tim hotel. Apakah ada hal lain yang bisa saya bantu?",
}

SERVICE_REQUEST_NOTED_PHRASES = {
    "en": "Thank you. I have noted your request, but the hotel request system is not connected right now. Is there anything else I can help with?",
    "zh": "谢谢。我已经记录您的请求，不过酒店请求系统目前未连接。还有其他可以帮您的吗？",
    "zh-yue": "多謝。我已經記低你嘅要求，不過酒店系統而家未連接。仲有冇其他可以幫你？",
    "ms": "Terima kasih. Saya telah mencatat permintaan anda, tetapi sistem permintaan hotel belum bersambung sekarang. Ada apa-apa lagi yang boleh saya bantu?",
    "id": "Terima kasih. Saya telah mencatat permintaan Anda, tetapi sistem permintaan hotel belum terhubung saat ini. Apakah ada hal lain yang bisa saya bantu?",
}

STILL_THERE_PHRASES = {
    "en": "Are you still there? Please let me know how I can help.",
    "zh": "请问您还在线吗？请告诉我有什么可以帮您。",
    "zh-yue": "請問你仲喺度嗎？有咩需要可以話我知。",
    "ms": "Adakah anda masih di talian? Sila beritahu saya bagaimana saya boleh membantu.",
    "id": "Apakah Anda masih di sana? Silakan beri tahu saya bagaimana saya bisa membantu.",
}


DEFAULT_GREETING_TEXT = "Hello, this is the AI assistant. How can I help you today?"
DEFAULT_PERSONAL_GREETING_TEXT = "Hello {caller_name}, my name is Nova, your personal AI Voicebot. How can I help you today?"
FOREIGN_SCRIPT_PATTERN = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u0600-\u06ff\u0900-\u097f\u0b80-\u0bff\u0e00-\u0e7f]"
)


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


def agi_response_is_dead_channel(response: str) -> bool:
    return response.startswith("511") or "dead channel" in response.lower()


def agi_result_code(response: str) -> int | None:
    marker = "result="
    if marker not in response:
        return None
    tail = response.split(marker, 1)[1].strip()
    token = tail.split(" ", 1)[0]
    try:
        return int(token)
    except ValueError:
        return None


def agi_answer() -> str:
    return agi_command("ANSWER")


def agi_stream_file(path_without_extension: str) -> str:
    return agi_command(f'STREAM FILE "{path_without_extension}" ""')


def agi_exec(app: str, args: str = "") -> str:
    return agi_command(f"EXEC {app} {args}".strip())


def agi_hangup() -> str:
    return agi_command("HANGUP")


def agi_goto(context: str, extension: str, priority: int = 1) -> str:
    return agi_command(f"SET CONTEXT {context}") + " | " + agi_command(f"SET EXTENSION {extension}") + " | " + agi_command(f"SET PRIORITY {priority}")


def agi_get_variable(name: str) -> str:
    return agi_command(f"GET VARIABLE {name}")


def agi_variable_value(response: str) -> str:
    if not response.startswith("200 result=1"):
        return ""
    if "(" not in response or ")" not in response:
        return ""
    return response.split("(", 1)[1].rsplit(")", 1)[0]


def agi_channel_status() -> str:
    return agi_command("CHANNEL STATUS")


def agi_channel_is_alive() -> tuple[bool, str]:
    response = agi_channel_status()
    if agi_response_is_dead_channel(response):
        return False, response
    # CHANNEL STATUS result 6 means the channel is up. Anything else is not a live call for this EAGI loop.
    return agi_result_code(response) == 6, response


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


def parse_sip_from_header(from_header: str) -> dict[str, str]:
    display_name = ""
    sip_user = ""
    header = from_header.strip()
    if '"' in header:
        parts = header.split('"')
        if len(parts) >= 3:
            display_name = parts[1].strip()
    elif "<" in header:
        display_name = header.split("<", 1)[0].strip()
    if "sip:" in header:
        sip_part = header.split("sip:", 1)[1]
        sip_user = sip_part.split("@", 1)[0].strip()
    return {"display_name": unquote(display_name), "sip_user": unquote(sip_user)}


def enrich_session_caller_details(session: CallSession) -> None:
    caller_name = agi_variable_value(agi_get_variable("AI_CALLER_NAME"))
    caller_num = agi_variable_value(agi_get_variable("AI_CALLER_NUM"))
    sip_from = agi_variable_value(agi_get_variable("AI_SIP_FROM"))
    parsed_from = parse_sip_from_header(sip_from)

    session.sip_from_header = sip_from
    if caller_name:
        session.caller_name = caller_name
    elif parsed_from.get("display_name"):
        session.caller_name = parsed_from["display_name"]

    if caller_num:
        session.caller_id = caller_num
    elif parsed_from.get("sip_user"):
        session.caller_id = parsed_from["sip_user"]


def greeting_caller_name(caller_name: str) -> str:
    name = (caller_name or "").strip()
    if not name:
        return ""
    for separator in (" - ", "|", "/"):
        if separator in name:
            name = name.split(separator, 1)[0].strip()
    name = re.sub(r"\s+", " ", name)
    if not re.search(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", name):
        return ""
    return name[:60]


def build_greeting_text(session: CallSession) -> tuple[str, bool]:
    caller_name = greeting_caller_name(session.caller_name)
    if caller_name and os.getenv("AI_GREETING_PERSONALIZE", "true").lower() == "true":
        template = os.getenv("AI_GREETING_PERSONAL_TEXT", DEFAULT_PERSONAL_GREETING_TEXT)
        return template.format(caller_name=caller_name), True
    return os.getenv("AI_GREETING_TEXT", DEFAULT_GREETING_TEXT), False


def audio_input_quality(pcm: bytes, sample_rate: int) -> dict[str, float]:
    return {
        "duration_seconds": round(pcm_duration_seconds(pcm, sample_rate), 3),
        "rms_dbfs": round(rms_dbfs(pcm), 1),
        "speech_ratio": round(
            speech_ratio(
                pcm,
                sample_rate,
                threshold_dbfs=float(os.getenv("AUDIO_SPEECH_THRESHOLD_DBFS", "-38")),
            ),
            3,
        ),
    }


def audio_input_should_be_ignored(pcm: bytes, sample_rate: int) -> tuple[bool, dict[str, float], str | None]:
    quality = audio_input_quality(pcm, sample_rate)
    min_seconds = float(os.getenv("MIN_AUDIO_SECONDS", "0.75"))
    min_rms = float(os.getenv("MIN_AUDIO_RMS_DBFS", "-46"))
    min_speech_ratio = float(os.getenv("MIN_AUDIO_SPEECH_RATIO", "0.08"))

    if quality["duration_seconds"] < min_seconds:
        return True, quality, f"audio shorter than {min_seconds:g}s"
    if quality["rms_dbfs"] < min_rms:
        return True, quality, f"audio rms below {min_rms:g} dBFS"
    if quality["speech_ratio"] < min_speech_ratio:
        return True, quality, f"speech ratio below {min_speech_ratio:g}"
    return False, quality, None


def transcript_should_be_ignored(
    transcript: str,
    detected_language: str,
    confidence: float,
    preferred_language: str,
) -> tuple[bool, str | None]:
    text = (transcript or "").strip()
    if not text:
        return True, "empty transcript"

    min_words = int(os.getenv("MIN_TRANSCRIPT_WORDS", "2"))
    switch_threshold = float(os.getenv("LANGUAGE_SWITCH_CONFIDENCE", "0.90"))
    short_chars = int(os.getenv("LOW_CONFIDENCE_TRANSCRIPT_MAX_CHARS", "16"))
    words = re.findall(r"[\w']+", text, flags=re.UNICODE)

    if detected_language != preferred_language and confidence < switch_threshold:
        if FOREIGN_SCRIPT_PATTERN.search(text) or len(text) <= short_chars or len(words) < min_words:
            return True, (
                f"low-confidence foreign transcript language={detected_language} "
                f"confidence={confidence:.2f} preferred={preferred_language}"
            )

    if (
        detected_language == preferred_language
        and preferred_language in {"zh", "zh-yue", "ja", "ko", "th", "ta", "hi", "ar"}
        and FOREIGN_SCRIPT_PATTERN.search(text)
    ):
        return False, None

    if detected_language != preferred_language and len(words) < min_words and len(text) <= short_chars and confidence < switch_threshold:
        return True, "short low-confidence transcript"

    return False, None


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
    if transfer_type == "room" and value.isdigit():
        return value
    return extension if extension.isdigit() else human_extension


def model_transfer_action_is_allowed(
    transcript: str,
    action: dict | None,
    human_extension: str,
    room_service_extension: str,
) -> tuple[bool, str | None]:
    """Only honor model transfer tool calls when the caller explicitly asked for transfer.

    The Realtime model can occasionally emit a transfer tool call while also asking a
    normal service-intake question. This guard keeps routine hotel service requests in
    the collection/confirmation flow unless the caller clearly requested a live team,
    a room-service team, a direct room transfer, or emergency escalation.
    """
    if not action:
        return False, "missing transfer action"

    normalized = transcript.lower()
    raw_extension = str(action.get("extension", "")).strip()
    transfer_type = (action.get("transfer_type") or "").strip().lower()
    target_extension = normalize_transfer_extension(raw_extension, transfer_type, human_extension, room_service_extension)
    effective_type = transfer_type or (
        "room_service"
        if target_extension == room_service_extension
        else "room"
        if target_extension.isdigit() and target_extension not in {str(human_extension), str(room_service_extension)}
        else "human"
    )

    deterministic = determine_transfer_action(
        transcript,
        human_extension=human_extension,
        room_service_extension=room_service_extension,
    )
    if deterministic and deterministic.get("transfer_type") == effective_type:
        return True, None

    if effective_type == "human":
        explicit_human = re.search(
            r"\b(transfer|connect|call|dial|reach|speak|talk|put me through|route)\b"
            r".*\b(1920|front desk|frontdesk|reception|receptionist|concierge|operator|human|agent|manager|person|staff)\b",
            normalized,
        )
        if explicit_human:
            return True, None
        return False, "caller did not explicitly request a human/front-desk transfer"

    if effective_type == "room_service":
        explicit_room_service = re.search(
            r"\b(transfer|connect|call|dial|reach|speak|talk|put me through|route)\b"
            r".*\b(1921|room service|in-room dining|in room dining)\b",
            normalized,
        )
        if explicit_room_service:
            return True, None
        return False, "room-service request is not an explicit request to transfer to room-service staff"

    if effective_type == "room":
        escaped_extension = re.escape(target_extension)
        explicit_room = re.search(
            rf"\b(transfer|connect|call|dial|reach|put me through|route)\b.*\b(room\s*)?{escaped_extension}\b",
            normalized,
        )
        if explicit_room:
            return True, None
        return False, "caller did not explicitly request a direct room transfer"

    return False, f"unknown transfer type {effective_type!r}"


def build_hotel_request_body(session: CallSession, payload: dict) -> dict:
    return {
        "call_id": session.call_id,
        "caller_id": session.caller_id,
        "caller_name": session.caller_name,
        "sip_from_header": session.sip_from_header,
        "preferred_language": session.preferred_language,
        "request": payload,
        "submitted_at": time.time(),
    }


def post_hotel_request(session: CallSession, payload: dict) -> dict:
    webhook_url = os.getenv("HOTEL_REQUEST_WEBHOOK_URL", "").strip()
    token = os.getenv("HOTEL_REQUEST_WEBHOOK_TOKEN", "").strip()
    body = build_hotel_request_body(session, payload)
    if not webhook_url:
        return {"sent": False, "reason": "HOTEL_REQUEST_WEBHOOK_URL is not configured", "payload": body}

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(webhook_url, data=encoded, headers=headers, method="POST")
    with request.urlopen(req, timeout=float(os.getenv("HOTEL_REQUEST_WEBHOOK_TIMEOUT_SECONDS", "8"))) as response:
        response_body = response.read(4096).decode("utf-8", errors="replace")
        return {
            "sent": 200 <= response.status < 300,
            "status": response.status,
            "response": response_body,
            "payload": body,
        }


def rainbow_service_request_destination(payload: dict) -> tuple[str, str]:
    category = (payload.get("category") or "").strip().lower()
    summary = (payload.get("summary") or "").strip().lower()
    notes = (payload.get("notes") or "").strip().lower()
    room_service_categories = {
        item.strip().lower()
        for item in os.getenv("RAINBOW_ROOM_SERVICE_CATEGORIES", "room_service").split(",")
        if item.strip()
    }
    if not room_service_categories:
        room_service_categories = {"room_service"}

    room_service_keywords = {
        item.strip().lower()
        for item in os.getenv(
            "RAINBOW_ROOM_SERVICE_KEYWORDS",
            "room service,in-room dining,in room dining,food,meal,breakfast,lunch,dinner,order,prawn,aglio,olio",
        ).split(",")
        if item.strip()
    }

    if category in room_service_categories or any(keyword in f"{summary} {notes}" for keyword in room_service_keywords):
        return "room_service", os.getenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "").strip()
    return "front_desk", os.getenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "").strip()


def notify_rainbow_service_request(session: CallSession, payload: dict) -> dict:
    enabled = os.getenv("RAINBOW_NODE_NOTIFICATIONS_ENABLED", "true").lower() == "true"
    destination, bubble_jid = rainbow_service_request_destination(payload)
    body = build_hotel_request_body(session, payload)
    body["rainbow_destination"] = destination

    if not enabled:
        return {"sent": False, "reason": "RAINBOW_NODE_NOTIFICATIONS_ENABLED is false", "destination": destination, "payload": body}
    if not bubble_jid:
        env_name = "RAINBOW_ROOM_SERVICE_BUBBLE_JID" if destination == "room_service" else "RAINBOW_FRONT_DESK_BUBBLE_JID"
        return {"sent": False, "reason": f"{env_name} is not configured", "destination": destination, "payload": body}

    required_env = ["RAINBOW_LOGIN", "RAINBOW_PASSWORD", "RAINBOW_APP_ID", "RAINBOW_APP_SECRET"]
    missing = [name for name in required_env if not os.getenv(name, "").strip()]
    if missing:
        return {"sent": False, "reason": f"Missing Rainbow SDK env vars: {', '.join(missing)}", "destination": destination, "payload": body}

    script = Path(os.getenv("RAINBOW_NODE_NOTIFIER_SCRIPT", Path(__file__).with_name("rainbow_service_request_notifier.js"))).resolve()
    if not script.exists():
        return {"sent": False, "reason": f"Rainbow notifier script not found: {script}", "destination": destination, "payload": body}

    command = [os.getenv("RAINBOW_NODE_BIN", "node"), str(script)]
    node_payload = dict(body)
    node_payload["bubble_jid"] = bubble_jid
    node_payload["destination"] = destination
    timeout = float(os.getenv("RAINBOW_NODE_TIMEOUT_SECONDS", "45"))

    try:
        completed = subprocess.run(
            command,
            input=json.dumps(node_payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"sent": False, "reason": str(exc), "destination": destination, "payload": body}
    except subprocess.TimeoutExpired:
        return {"sent": False, "reason": f"Rainbow notifier timed out after {timeout:g}s", "destination": destination, "payload": body}
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    stdout_lines = [line for line in stdout.splitlines() if line.strip()]
    parsed_stdout: dict[str, object] = {}
    sdk_log_lines = stdout_lines
    for index in range(len(stdout_lines) - 1, -1, -1):
        candidate = stdout_lines[index].strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            parsed_stdout = parsed
            sdk_log_lines = stdout_lines[:index] + stdout_lines[index + 1 :]
            break

    result = {
        "sent": bool(parsed_stdout.get("sent", completed.returncode == 0)),
        "destination": destination,
        "bubble_jid": bubble_jid,
        "message_id": parsed_stdout.get("message_id"),
        "returncode": completed.returncode,
        "stdout": json.dumps(parsed_stdout, ensure_ascii=False) if parsed_stdout else stdout[-4096:],
        "sdk_log": "\n".join(sdk_log_lines)[-4096:],
        "stderr": stderr[-4096:],
        "payload": body,
    }
    if completed.returncode != 0 and not stdout and stderr:
        result["reason"] = stderr.splitlines()[-1]
    return result


def append_rainbow_service_request_result(session: CallSession, payload: dict) -> None:
    try:
        result = notify_rainbow_service_request(session, payload)
    except Exception as exc:
        destination, _bubble_jid = rainbow_service_request_destination(payload)
        result = {
            "sent": False,
            "reason": str(exc),
            "destination": destination,
            "payload": build_hotel_request_body(session, payload),
        }
    session.append_event("service_request_rainbow_result", result)


async def submit_service_request_notifications(session: CallSession, payload: dict) -> tuple[dict, dict]:
    try:
        webhook_result = await asyncio.to_thread(post_hotel_request, session, payload)
    except Exception as exc:
        webhook_result = {"sent": False, "reason": str(exc), "payload": build_hotel_request_body(session, payload)}

    if os.getenv("RAINBOW_NODE_ASYNC", "true").lower() == "true":
        destination, bubble_jid = rainbow_service_request_destination(payload)
        threading.Thread(
            target=append_rainbow_service_request_result,
            args=(session, payload),
            daemon=True,
            name=f"rainbow-notifier-{session.call_id}",
        ).start()
        return webhook_result, {
            "queued": True,
            "sent": False,
            "destination": destination,
            "bubble_jid": bubble_jid,
            "reason": "Rainbow notification queued asynchronously",
            "payload": build_hotel_request_body(session, payload),
        }

    try:
        rainbow_result = await asyncio.to_thread(notify_rainbow_service_request, session, payload)
    except Exception as exc:
        destination, _bubble_jid = rainbow_service_request_destination(payload)
        rainbow_result = {
            "sent": False,
            "reason": str(exc),
            "destination": destination,
            "payload": build_hotel_request_body(session, payload),
        }
    return webhook_result, rainbow_result


def transcript_text(session: CallSession) -> str:
    lines = [
        f"Call ID: {session.call_id}",
        f"Caller ID: {session.caller_id}",
        f"Caller name: {session.caller_name}",
        f"Preferred language: {session.preferred_language}",
        "",
        "Transcript:",
    ]
    for event in session.history:
        event_type = event.get("type")
        if event_type == "user":
            lines.append(f"Guest: {event.get('text', '')}")
        elif event_type == "assistant":
            lines.append(f"Assistant: {event.get('text', '')}")
        elif event_type == "language_change":
            lines.append(f"[Language changed to {event.get('language')} confidence={event.get('confidence')}]")
        elif event_type == "transfer_result":
            lines.append(f"[Transfer result: {event.get('target')} {event.get('status')} {event.get('status_protocol')}]")
        elif event_type == "service_request_submitted":
            lines.append(f"[Service request submitted: sent={event.get('sent')} status={event.get('status', '')}]")
        elif event_type == "service_request_rainbow_result":
            lines.append(f"[Rainbow notification: sent={event.get('sent')} destination={event.get('destination', '')}]")
    return "\n".join(lines).strip() + "\n"


def send_call_transcript_email(session: CallSession) -> dict:
    enabled = os.getenv("EMAIL_TRANSCRIPT_ENABLED", "true").lower() == "true"
    recipient = os.getenv("TRANSCRIPT_EMAIL_TO", "kahyean.yip+pdopenai@gmail.com").strip()
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not enabled:
        return {"sent": False, "reason": "EMAIL_TRANSCRIPT_ENABLED is false"}
    if not recipient:
        return {"sent": False, "reason": "TRANSCRIPT_EMAIL_TO is not configured"}
    if not smtp_host:
        return {"sent": False, "reason": "SMTP_HOST is not configured"}

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
    sender = os.getenv("TRANSCRIPT_EMAIL_FROM", smtp_user or "aivoicebot@localhost").strip()
    sender_name = os.getenv("EMAIL_FROM_NAME", "Hotel Voicebot").strip()
    sender_header = formataddr((sender_name, sender)) if sender_name else sender

    msg = EmailMessage()
    msg["From"] = sender_header
    msg["To"] = recipient
    msg["Subject"] = f"AI Voice Bot Call Transcript - {session.call_id}"
    msg.set_content(transcript_text(session))

    log_path = session.log_dir / f"{session.call_id}.jsonl"
    if log_path.exists():
        msg.add_attachment(
            log_path.read_bytes(),
            maintype="application",
            subtype="jsonl",
            filename=f"{session.call_id}.jsonl",
        )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=float(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))) as smtp:
        if smtp_starttls:
            smtp.starttls()
        if smtp_user or smtp_password:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    return {"sent": True, "to": recipient, "from": sender_header, "host": smtp_host, "port": smtp_port}


def smtp_config(prefix: str = "") -> dict:
    recipient = os.getenv(f"{prefix}EMAIL_TO", "").strip() or os.getenv("TRANSCRIPT_EMAIL_TO", "").strip()
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
    sender = os.getenv("TRANSCRIPT_EMAIL_FROM", smtp_user or "aivoicebot@localhost").strip()
    sender_name = os.getenv("EMAIL_FROM_NAME", "Hotel Voicebot").strip()
    sender_header = formataddr((sender_name, sender)) if sender_name else sender
    return {
        "recipient": recipient,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "smtp_starttls": smtp_starttls,
        "sender": sender,
        "sender_header": sender_header,
    }


def send_service_request_email(session: CallSession, request_payload: dict, submission: dict) -> dict:
    enabled = os.getenv("SERVICE_REQUEST_EMAIL_ENABLED", "true").lower() == "true"
    if not enabled:
        return {"sent": False, "reason": "SERVICE_REQUEST_EMAIL_ENABLED is false"}

    config = smtp_config("SERVICE_REQUEST_")
    if not config["recipient"]:
        return {"sent": False, "reason": "TRANSCRIPT_EMAIL_TO is not configured"}
    if not config["smtp_host"]:
        return {"sent": False, "reason": "SMTP_HOST is not configured"}

    request_details = json.dumps(request_payload, ensure_ascii=False, indent=2)
    submission_details = json.dumps(
        {key: value for key, value in submission.items() if key != "payload"},
        ensure_ascii=False,
        indent=2,
    )
    body = "\n".join(
        [
            "A confirmed hotel service request was submitted.",
            "",
            f"Call ID: {session.call_id}",
            f"Caller ID: {session.caller_id}",
            f"Caller name: {session.caller_name}",
            f"Preferred language: {session.preferred_language}",
            "",
            "Request:",
            request_details,
            "",
            "Webhook result:",
            submission_details,
            "",
            "Recent transcript:",
            transcript_text(session),
        ]
    )

    msg = EmailMessage()
    msg["From"] = config["sender_header"]
    msg["To"] = config["recipient"]
    category = request_payload.get("category", "service_request")
    room = request_payload.get("room_number", "")
    room_suffix = f" room {room}" if room else ""
    msg["Subject"] = f"Hotel Service Request - {category}{room_suffix} - {session.call_id}"
    msg.set_content(body)

    with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=float(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))) as smtp:
        if config["smtp_starttls"]:
            smtp.starttls()
        if config["smtp_user"] or config["smtp_password"]:
            smtp.login(config["smtp_user"], config["smtp_password"])
        smtp.send_message(msg)

    return {
        "sent": True,
        "to": config["recipient"],
        "from": config["sender_header"],
        "host": config["smtp_host"],
        "port": config["smtp_port"],
    }


def finalize_transcript_email(session: CallSession) -> None:
    global TRANSCRIPT_EMAIL_SENT
    if TRANSCRIPT_EMAIL_SENT:
        return
    TRANSCRIPT_EMAIL_SENT = True
    try:
        email_result = send_call_transcript_email(session)
        session.append_event("transcript_email_result", email_result)
    except Exception as exc:
        LOGGER.exception("Could not send transcript email")
        session.append_event("transcript_email_error", {"error": str(exc)})


def append_service_request_email_result(session: CallSession, action: dict, submission: dict) -> None:
    try:
        service_email_result = send_service_request_email(session, action, submission)
        session.append_event("service_request_email_result", service_email_result)
    except Exception as exc:
        LOGGER.exception("Could not send service request email")
        session.append_event("service_request_email_error", {"error": str(exc)})


def handle_shutdown_signal(signum: int, _frame: object) -> None:
    if CURRENT_SESSION is not None:
        CURRENT_SESSION.append_event("hangup_signal", {"signal": signum})
        finalize_transcript_email(CURRENT_SESSION)
    raise SystemExit(0)


def service_request_is_confirmed(action: dict | None) -> bool:
    if not action:
        return False
    return action.get("confirmed_with_guest") is True


def service_request_confirmation_text(action: dict, language: str) -> str:
    summary = action.get("summary", "this request")
    room = action.get("room_number")
    if language == "zh":
        room_text = f"，房号 {room}" if room else ""
        return f"请确认：您是否要我提交这个请求：{summary}{room_text}？"
    if language == "zh-yue":
        room_text = f"，房號 {room}" if room else ""
        return f"請確認：你係咪想我提交呢個要求：{summary}{room_text}？"
    if language == "ms":
        room_text = f" untuk bilik {room}" if room else ""
        return f"Sila sahkan: adakah anda mahu saya hantar permintaan ini, {summary}{room_text}?"
    if language == "id":
        room_text = f" untuk kamar {room}" if room else ""
        return f"Mohon konfirmasi: apakah Anda ingin saya mengirim permintaan ini, {summary}{room_text}?"
    room_text = f" for room {room}" if room else ""
    return f"Please confirm: would you like me to submit this request, {summary}{room_text}?"


def transfer_reclaim_enabled() -> bool:
    return os.getenv("TRANSFER_RECLAIM_ON_FAILURE", "true").lower() == "true"


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
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Read this approved hotel phone phrase verbatim as natural speech. "
                                    "Do not add, remove, translate, apologize, refuse, or explain. "
                                    f"Phrase: {text}"
                                ),
                            }
                        ],
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "output_modalities": ["audio"],
                        "instructions": (
                            "You are only performing text-to-speech for an approved hotel phone system phrase. "
                            "Speak the phrase exactly and naturally. Do not refuse or mention policy."
                        ),
                    },
                }
            )
        )
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") in {"response.audio.delta", "response.output_audio.delta"}:
                pcm24k += base64.b64decode(event.get("delta", ""))
            elif event.get("type") == "response.done":
                break
            elif event.get("type") == "error":
                raise RuntimeError(f"OpenAI phrase synthesis failed: {event.get('error', event)}")

    return asterisk_playback_wav(target_wav, pcm24k)


async def synthesize_cached_greeting(
    client: OpenAIRealtimeClient,
    session: CallSession,
    text: str,
    sounds_dir: Path,
) -> Path:
    """Generate the greeting once and reuse it so callers hear the same greeting every call."""
    cache_enabled = os.getenv("AI_GREETING_CACHE", "true").lower() == "true"
    cache_version = os.getenv("AI_GREETING_CACHE_VERSION", "2")
    cache_key_source = f"{cache_version}|{client.model}|{session.preferred_language}|{text}"
    cache_key = hashlib.sha256(cache_key_source.encode("utf-8")).hexdigest()[:16]
    cached_wav = sounds_dir / f"greeting_{cache_key}.wav"

    if cache_enabled and cached_wav.exists() and cached_wav.stat().st_size > 44:
        session.append_event("greeting_cache_hit", {"file": str(cached_wav), "text": text})
        return cached_wav

    target_wav = cached_wav if cache_enabled else sounds_dir / f"{session.call_id}_greeting.wav"
    generated_wav = await synthesize_text_phrase(client, session, text, target_wav)
    if cache_enabled:
        session.append_event("greeting_cache_created", {"file": str(generated_wav), "text": text})
    return generated_wav


async def synthesize_cached_phrase(
    client: OpenAIRealtimeClient,
    session: CallSession,
    text: str,
    sounds_dir: Path,
    prefix: str,
) -> Path:
    cache_enabled = os.getenv("AI_PHRASE_CACHE", "true").lower() == "true"
    cache_version = os.getenv("AI_PHRASE_CACHE_VERSION", "1")
    cache_key_source = f"{cache_version}|{client.model}|{session.preferred_language}|{text}"
    cache_key = hashlib.sha256(cache_key_source.encode("utf-8")).hexdigest()[:16]
    cached_wav = sounds_dir / f"{prefix}_{cache_key}.wav"

    if cache_enabled and cached_wav.exists() and cached_wav.stat().st_size > 44:
        session.append_event("phrase_cache_hit", {"file": str(cached_wav), "prefix": prefix, "text": text})
        return cached_wav

    target_wav = cached_wav if cache_enabled else sounds_dir / f"{session.call_id}_{prefix}.wav"
    generated_wav = await synthesize_text_phrase(client, session, text, target_wav)
    if cache_enabled:
        session.append_event("phrase_cache_created", {"file": str(generated_wav), "prefix": prefix, "text": text})
    return generated_wav


async def synthesize_transfer_phrase(client: OpenAIRealtimeClient, session: CallSession, text: str, sounds_dir: Path, turn: int) -> Path:
    return await synthesize_text_phrase(client, session, text, sounds_dir / f"{session.call_id}_{turn}_transfer.wav")


async def run_call() -> int:
    global CURRENT_SESSION
    agi_env = parse_agi_env()
    default_language = os.getenv("DEFAULT_LANGUAGE", "en")
    session = CallSession.from_agi_env(agi_env, default_language)
    CURRENT_SESSION = session
    sounds_dir = Path(os.getenv("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/ai"))
    sounds_dir.mkdir(parents=True, exist_ok=True)
    record_audio = os.getenv("RECORD_AUDIO", "false").lower() == "true"
    silence_timeout_ms = int(os.getenv("SILENCE_TIMEOUT_MS", "900"))
    max_utterance_seconds = int(os.getenv("MAX_UTTERANCE_SECONDS", "15"))
    no_speech_timeout_seconds = float(os.getenv("NO_SPEECH_TIMEOUT_SECONDS", "5"))
    transfer_extension = os.getenv("TRANSFER_EXTENSION", os.getenv("HUMAN_TRANSFER_EXTENSION", "1920"))
    room_service_transfer_extension = os.getenv("ROOM_SERVICE_TRANSFER_EXTENSION", "1921")
    source_codec = os.getenv("ASTERISK_EAGI_CODEC", "slin")
    source_rate = int(os.getenv("ASTERISK_EAGI_SAMPLE_RATE", "8000"))
    enable_ready_tone = os.getenv("ENABLE_READY_TONE", "false").lower() == "true"
    enable_ai_greeting = os.getenv("ENABLE_AI_GREETING", "true").lower() == "true"
    no_audio_max_turns = int(os.getenv("NO_AUDIO_MAX_TURNS", "20"))
    silence_prompt_after_turns = int(os.getenv("SILENCE_PROMPT_AFTER_TURNS", "2"))
    max_silence_prompts = int(os.getenv("MAX_SILENCE_PROMPTS", "2"))
    eagi_frame_bytes = int(source_rate * 20 / 1000) * (2 if source_codec.lower() in {"slin", "slin16", "pcm16"} else 1)
    drain_after_playback_ms = int(os.getenv("EAGI_DRAIN_AFTER_PLAYBACK_MS", "250"))

    agi_verbose(f"AI assistant call started id={session.call_id}", 1)
    agi_answer()
    enrich_session_caller_details(session)
    greeting_text, greeting_personalized = build_greeting_text(session)
    session.append_event(
        "call_started",
        {
            "caller_id": session.caller_id,
            "caller_name": session.caller_name,
            "sip_from_header": session.sip_from_header,
            "greeting_personalized": greeting_personalized,
            "codec": source_codec,
            "sample_rate": source_rate,
        },
    )

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
            agi_verbose("AI assistant preparing greeting", 1)
            greeting_wav = await synthesize_cached_greeting(client, session, greeting_text, sounds_dir)
            greeting_response = agi_stream_file(str(greeting_wav.with_suffix("")))
            session.append_event(
                "greeting_played",
                {
                    "file": str(greeting_wav),
                    "text": greeting_text,
                    "personalized": greeting_personalized,
                    "agi_response": greeting_response,
                },
            )
            drained = drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)
            if drained:
                session.append_event("eagi_audio_drained", {"after": "greeting", "bytes": drained})
        except Exception as exc:
            LOGGER.exception("Could not synthesize or play AI greeting")
            session.append_event("greeting_error", {"error": str(exc)})

    no_audio_turns = 0
    ignored_audio_turns = 0
    silence_prompt_count = 0

    for turn in range(1, 50):
        try:
            alive, status_response = agi_channel_is_alive()
            if not alive:
                session.append_event("hangup", {"reason": "channel not alive before listening", "channel_status": status_response})
                break

            verbose_response = agi_verbose(f"AI assistant listening turn={turn}", 1)
            if agi_response_is_dead_channel(verbose_response):
                session.append_event("hangup", {"reason": "dead channel before listening", "agi_response": verbose_response})
                break

            pcm = capture_utterance_from_fd3(
                fd=3,
                codec=source_codec,
                sample_rate=source_rate,
                silence_timeout_ms=silence_timeout_ms,
                max_seconds=max_utterance_seconds,
                no_speech_timeout_seconds=no_speech_timeout_seconds,
            )
            ignore_audio, quality, audio_ignore_reason = audio_input_should_be_ignored(pcm, source_rate)
            session.append_event("audio_captured", {"turn": turn, "bytes": len(pcm), **quality})
            if len(pcm) < source_rate * 2 * 0.25:
                agi_verbose(f"AI assistant ignored short audio bytes={len(pcm)}", 1)
                if len(pcm) == 0:
                    no_audio_turns += 1
                    ignored_audio_turns += 1
                    session.append_event(
                        "no_eagi_audio",
                        {
                            "turn": turn,
                            "count": no_audio_turns,
                            "hint": "No bytes arrived on EAGI fd 3. Check caller microphone, inbound RTP, NAT/firewall, and RTP debug.",
                        },
                    )
                    alive, status_response = agi_channel_is_alive()
                    if not alive:
                        session.append_event("hangup", {"reason": "channel not alive after empty audio", "channel_status": status_response})
                        break
                    if no_audio_turns >= no_audio_max_turns:
                        break
                    if ignored_audio_turns >= silence_prompt_after_turns and silence_prompt_count < max_silence_prompts:
                        silence_prompt_count += 1
                        ignored_audio_turns = 0
                        phrase = STILL_THERE_PHRASES.get(session.preferred_language, STILL_THERE_PHRASES["en"])
                        prompt_wav = await synthesize_cached_phrase(client, session, phrase, sounds_dir, "still_there")
                        prompt_response = agi_stream_file(str(prompt_wav.with_suffix("")))
                        session.append_event(
                            "silence_prompt_played",
                            {
                                "turn": turn,
                                "count": silence_prompt_count,
                                "file": str(prompt_wav),
                                "text": phrase,
                                "agi_response": prompt_response,
                            },
                        )
                        if agi_response_is_dead_channel(prompt_response):
                            session.append_event("hangup", {"reason": "dead channel during silence prompt playback"})
                            break
                        drained = drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)
                        if drained:
                            session.append_event("eagi_audio_drained", {"after": "silence_prompt", "turn": turn, "bytes": drained})
                    continue
                continue
            if ignore_audio:
                ignored_audio_turns += 1
                agi_verbose(f"AI assistant ignored weak audio reason={audio_ignore_reason} bytes={len(pcm)}", 1)
                session.append_event(
                    "audio_ignored",
                    {"turn": turn, "bytes": len(pcm), "reason": audio_ignore_reason, **quality},
                )
                if ignored_audio_turns >= silence_prompt_after_turns and silence_prompt_count < max_silence_prompts:
                    silence_prompt_count += 1
                    ignored_audio_turns = 0
                    phrase = STILL_THERE_PHRASES.get(session.preferred_language, STILL_THERE_PHRASES["en"])
                    prompt_wav = await synthesize_cached_phrase(client, session, phrase, sounds_dir, "still_there")
                    prompt_response = agi_stream_file(str(prompt_wav.with_suffix("")))
                    session.append_event(
                        "silence_prompt_played",
                        {
                            "turn": turn,
                            "count": silence_prompt_count,
                            "file": str(prompt_wav),
                            "text": phrase,
                            "agi_response": prompt_response,
                        },
                    )
                    if agi_response_is_dead_channel(prompt_response):
                        session.append_event("hangup", {"reason": "dead channel during silence prompt playback"})
                        break
                    drained = drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)
                    if drained:
                        session.append_event("eagi_audio_drained", {"after": "silence_prompt", "turn": turn, "bytes": drained})
                continue
            no_audio_turns = 0
            ignored_audio_turns = 0

            if record_audio:
                write_wav(sounds_dir / f"{session.call_id}_{turn}_caller.wav", pcm, source_rate)

            agi_verbose(f"AI assistant sending turn={turn} to OpenAI bytes={len(pcm)}", 1)
            pcm24k = openai_input_pcm(pcm, source_rate)
            result = await client.process_turn(session, pcm24k)
            language, confidence = detect_language_from_text(result.transcript, session.preferred_language)
            ignore_transcript, transcript_ignore_reason = transcript_should_be_ignored(
                result.transcript,
                language,
                confidence,
                session.preferred_language,
            )
            if ignore_transcript:
                session.append_event(
                    "transcript_ignored",
                    {
                        "turn": turn,
                        "text": result.transcript,
                        "detected_language": language,
                        "confidence": confidence,
                        "preferred_language": session.preferred_language,
                        "reason": transcript_ignore_reason,
                    },
                )
                continue
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

            should_end_call, end_call_reason = should_end_call_deterministic(result.transcript)
            if should_end_call and result.end_call_action is None:
                result.end_call_action = {
                    "action": "end_call",
                    "reason": end_call_reason or "guest indicated there are no more requests",
                }

            transfer, reason = should_transfer_deterministic(result.transcript, session.failed_intent_count)
            target_extension = transfer_extension
            transfer_type = "human"
            if result.transfer_action:
                allowed, suppressed_reason = model_transfer_action_is_allowed(
                    result.transcript,
                    result.transfer_action,
                    transfer_extension,
                    room_service_transfer_extension,
                )
                if not allowed:
                    session.append_event(
                        "transfer_action_suppressed",
                        {
                            "reason": suppressed_reason,
                            "model_action": result.transfer_action,
                            "transcript": result.transcript,
                        },
                    )
                    result.transfer_action = None
                else:
                    transfer = True
                    reason = result.transfer_action.get("reason", "model requested transfer")
                    target_extension = result.transfer_action.get("extension", transfer_extension)
                    transfer_type = result.transfer_action.get("transfer_type") or (
                        "room_service"
                        if target_extension == room_service_transfer_extension
                        else "room"
                        if str(target_extension).isdigit() and str(target_extension) not in {str(transfer_extension), str(room_service_transfer_extension)}
                        else "human"
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
            if result.service_request_action:
                session.append_event("service_request_action_detected", result.service_request_action)
            if result.end_call_action:
                session.append_event("end_call_action_detected", result.end_call_action)

            if result.response_audio_pcm24k:
                response_wav = asterisk_playback_wav(sounds_dir / f"{session.call_id}_{turn}.wav", result.response_audio_pcm24k)
                playback_response = agi_stream_file(str(response_wav.with_suffix("")))
                session.append_event("assistant_audio_played", {"turn": turn, "file": str(response_wav), "agi_response": playback_response})
                if agi_response_is_dead_channel(playback_response):
                    session.append_event("hangup", {"reason": "dead channel during assistant playback"})
                    break
                drained = drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)
                if drained:
                    session.append_event("eagi_audio_drained", {"after": "assistant_playback", "turn": turn, "bytes": drained})
                time.sleep(0.25)
            else:
                session.append_event("assistant_audio_missing", {"turn": turn, "text": result.response_text})
                if not transfer and not result.service_request_action and not result.end_call_action:
                    continue

            if result.service_request_action:
                if not service_request_is_confirmed(result.service_request_action):
                    session.append_event(
                        "service_request_confirmation_required",
                        {
                            "reason": "model attempted to submit before explicit guest confirmation",
                            "request": result.service_request_action,
                        },
                    )
                    if not result.response_audio_pcm24k:
                        confirmation_question = service_request_confirmation_text(
                            result.service_request_action,
                            session.preferred_language,
                        )
                        confirmation_wav = await synthesize_text_phrase(
                            client,
                            session,
                            confirmation_question,
                            sounds_dir / f"{session.call_id}_{turn}_request_confirm_question.wav",
                        )
                        response = agi_stream_file(str(confirmation_wav.with_suffix("")))
                        session.append_event(
                            "service_request_confirmation_prompt_played",
                            {"turn": turn, "file": str(confirmation_wav), "text": confirmation_question, "agi_response": response},
                        )
                    continue
                try:
                    submission, rainbow_submission = await submit_service_request_notifications(session, result.service_request_action)
                    session.append_event("service_request_submitted", submission)
                    if rainbow_submission.get("queued"):
                        session.append_event("service_request_rainbow_queued", rainbow_submission)
                    else:
                        session.append_event("service_request_rainbow_result", rainbow_submission)
                    if submission.get("sent"):
                        confirmation = SERVICE_REQUEST_SUBMITTED_PHRASES.get(
                            session.preferred_language,
                            SERVICE_REQUEST_SUBMITTED_PHRASES["en"],
                        )
                    else:
                        confirmation = SERVICE_REQUEST_NOTED_PHRASES.get(
                            session.preferred_language,
                            SERVICE_REQUEST_NOTED_PHRASES["en"],
                        )
                    confirmation_wav = await synthesize_cached_phrase(
                        client,
                        session,
                        confirmation,
                        sounds_dir,
                        "request_submitted",
                    )
                    confirmation_response = agi_stream_file(str(confirmation_wav.with_suffix("")))
                    session.append_event(
                        "service_request_submission_prompt_played",
                        {
                            "turn": turn,
                            "file": str(confirmation_wav),
                            "text": confirmation,
                            "agi_response": confirmation_response,
                        },
                    )
                    if agi_response_is_dead_channel(confirmation_response):
                        session.append_event("hangup", {"reason": "dead channel during service request confirmation playback"})
                        break
                    drained = drain_audio_fd(3, eagi_frame_bytes, drain_after_playback_ms)
                    if drained:
                        session.append_event("eagi_audio_drained", {"after": "service_request_confirmation", "turn": turn, "bytes": drained})
                    threading.Thread(
                        target=append_service_request_email_result,
                        args=(session, result.service_request_action, submission),
                        daemon=True,
                        name=f"service-email-{session.call_id}",
                    ).start()
                except Exception as exc:
                    LOGGER.exception("Could not submit hotel request")
                    session.append_event("service_request_error", {"error": str(exc), "payload": result.service_request_action})
                if result.end_call_action:
                    session.append_event(
                        "call_closing",
                        {"reason": result.end_call_action.get("reason", "guest indicated there are no more requests")},
                    )
                    break
                continue

            if result.end_call_action:
                if not result.response_audio_pcm24k:
                    closing = CLOSING_PHRASES.get(session.preferred_language, CLOSING_PHRASES["en"])
                    closing_wav = await synthesize_text_phrase(
                        client,
                        session,
                        closing,
                        sounds_dir / f"{session.call_id}_{turn}_closing.wav",
                    )
                    closing_response = agi_stream_file(str(closing_wav.with_suffix("")))
                    session.append_event(
                        "closing_phrase_played",
                        {"turn": turn, "file": str(closing_wav), "text": closing, "agi_response": closing_response},
                    )
                    if agi_response_is_dead_channel(closing_response):
                        session.append_event("hangup", {"reason": "dead channel during closing phrase playback"})
                        break
                session.append_event(
                    "call_closing",
                    {"reason": result.end_call_action.get("reason", "guest indicated there are no more requests")},
                )
                break

            if transfer:
                session.request_transfer(reason or "transfer requested")
                if transfer_type == "room_service":
                    phrase_map = ROOM_SERVICE_TRANSFER_PHRASES
                elif transfer_type == "room":
                    phrase_map = ROOM_TRANSFER_PHRASES
                else:
                    phrase_map = TRANSFER_PHRASES
                phrase = phrase_map.get(session.preferred_language, phrase_map["en"])
                try:
                    transfer_wav = await synthesize_transfer_phrase(client, session, phrase, sounds_dir, turn)
                    transfer_phrase_response = agi_stream_file(str(transfer_wav.with_suffix("")))
                    if agi_response_is_dead_channel(transfer_phrase_response):
                        session.append_event("hangup", {"reason": "dead channel during transfer phrase playback"})
                        break
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
                    try:
                        channel_status = agi_channel_status()
                        session.append_event("channel_status_after_transfer_failure", {"response": channel_status})
                        if agi_response_is_dead_channel(channel_status):
                            session.append_event("transfer_reclaim_unavailable", {"reason": "dead channel after transfer failure"})
                            session.append_event("hangup", {"reason": "dead channel after transfer failure"})
                            break
                    except Exception as exc:
                        session.append_event("hangup", {"reason": "channel status failed after transfer", "error": str(exc)})
                        break
                    if transfer_reclaim_enabled():
                        reclaim_response = agi_goto("internal", "reclaim", 1)
                        session.append_event("transfer_reclaim_requested", {"response": reclaim_response})
                        break
                    continue
                break

        except (BrokenPipeError, KeyboardInterrupt):
            session.append_event("hangup", {"reason": "caller disconnected"})
            break
        except Exception as exc:
            LOGGER.error("Turn failed: %s\n%s", exc, traceback.format_exc())
            session.append_event("error", {"error": str(exc)})
            break

    finalize_transcript_email(session)

    try:
        if not (session.history and session.history[-1].get("type") == "transfer_reclaim_requested"):
            agi_hangup()
    except Exception:
        LOGGER.debug("Could not send AGI HANGUP after call ended", exc_info=True)
    return 0


def main() -> int:
    signal.signal(signal.SIGHUP, handle_shutdown_signal)
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    try:
        return asyncio.run(run_call())
    except Exception as exc:
        LOGGER.error("Fatal EAGI failure: %s\n%s", exc, traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
