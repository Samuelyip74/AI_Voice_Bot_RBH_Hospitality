from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_LANGUAGES = {
    "en": "English",
    "zh": "Mandarin Chinese",
    "zh-yue": "Cantonese",
    "ms": "Malay",
    "ta": "Tamil",
    "ja": "Japanese",
    "ko": "Korean",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
}

LANGUAGE_RESPONSE_INSTRUCTIONS = {
    "en": "Respond only in English.",
    "zh": "Respond only in Mandarin Chinese. Use Simplified Chinese unless the caller clearly uses Traditional Chinese.",
    "zh-yue": "Respond only in Cantonese.",
    "ms": "Respond only in Malay.",
    "ta": "Respond only in Tamil.",
    "ja": "Respond only in Japanese.",
    "ko": "Respond only in Korean.",
    "th": "Respond only in Thai.",
    "vi": "Respond only in Vietnamese.",
    "id": "Respond only in Indonesian.",
}

TRANSFER_TRIGGER_PATTERNS = [
    r"\bhuman agent\b",
    r"\boperator\b",
    r"\btransfer me\b",
    r"\breception\b",
    r"\breceptionist\b",
    r"\bfront desk\b",
    r"\bconcierge team\b",
    r"\bmanager\b",
    r"\blive agent\b",
    r"\bhuman support\b",
    r"\bstaff member\b",
    r"\bconnect me to reception\b",
    r"\btalk to (a )?(person|human|agent|representative)\b",
    r"\bspeak to (a )?(person|human|agent|representative)\b",
    r"\breal person\b",
]

ROOM_SERVICE_TRIGGER_PATTERNS = [
    r"\broom service\b",
    r"\bin-room dining\b",
    r"\border food\b",
    r"\bsend food\b",
    r"\bbreakfast in (my|the) room\b",
    r"\bdining delivery\b",
    r"\bfood to my room\b",
    r"\bmodify .* dining order\b",
    r"\bcancel .* dining order\b",
    r"\bcheck .* dining order\b",
    r"\bcomplain .* dining order\b",
]

URGENT_TRANSFER_PATTERNS = [
    r"\bemergency\b",
    r"\bmedical\b",
    r"\bdoctor\b",
    r"\bambulance\b",
    r"\bsecurity\b",
    r"\bunsafe\b",
    r"\bfire\b",
    r"\bpolice\b",
    r"\burgent\b",
]

LANGUAGE_HINTS = [
    ("zh-yue", ["唔", "咩", "係", "冇", "廣東話", "粤语", "粵語"]),
    ("zh", ["你好", "谢谢", "請", "请", "中文", "普通话", "嗎", "吗"]),
    ("ms", ["terima kasih", "bahasa melayu", "tolong", "saya mahu", "apa khabar"]),
    ("ta", ["வணக்கம்", "நன்றி", "தமிழ்", "தயவு செய்து"]),
    ("ja", ["こんにちは", "ありがとう", "日本語", "お願いします"]),
    ("ko", ["안녕하세요", "감사합니다", "한국어", "주세요"]),
    ("th", ["สวัสดี", "ขอบคุณ", "ภาษาไทย", "กรุณา"]),
    ("vi", ["xin chào", "cảm ơn", "tiếng việt", "vui lòng"]),
    ("id", ["terima kasih", "bahasa indonesia", "tolong", "saya ingin"]),
]


def detect_language_from_text(text: str, default: str = "en") -> tuple[str, float]:
    """Small deterministic language detector used as a guardrail around model metadata."""
    normalized = text.strip().lower()
    if not normalized:
        return default, 0.0

    for code, hints in LANGUAGE_HINTS:
        if any(hint.lower() in normalized for hint in hints):
            return code, 0.88

    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh", 0.74
    if re.search(r"[\u0b80-\u0bff]", text):
        return "ta", 0.86
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja", 0.86
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko", 0.86
    if re.search(r"[\u0e00-\u0e7f]", text):
        return "th", 0.86

    return default, 0.55


def language_response_instruction(language: str) -> str:
    return LANGUAGE_RESPONSE_INSTRUCTIONS.get(language, LANGUAGE_RESPONSE_INSTRUCTIONS["en"])


def should_transfer_deterministic(text: str, failed_intent_count: int = 0, angry: bool = False) -> tuple[bool, str | None]:
    action = determine_transfer_action(text, failed_intent_count=failed_intent_count, angry=angry)
    if action:
        return True, action["reason"]
    return False, None


def determine_transfer_action(
    text: str,
    failed_intent_count: int = 0,
    angry: bool = False,
    human_extension: str = "1920",
    room_service_extension: str = "1921",
) -> dict[str, str] | None:
    normalized = text.lower()
    for pattern in ROOM_SERVICE_TRIGGER_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "action": "transfer",
                "extension": room_service_extension,
                "reason": "guest requested room service or in-room dining",
                "transfer_type": "room_service",
            }
    for pattern in TRANSFER_TRIGGER_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "action": "transfer",
                "extension": human_extension,
                "reason": "guest requested a human concierge transfer",
                "transfer_type": "human",
            }
    for pattern in URGENT_TRANSFER_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "action": "transfer",
                "extension": human_extension,
                "reason": "guest request sounds urgent or safety-related",
                "transfer_type": "human",
            }
    if angry:
        return {
            "action": "transfer",
            "extension": human_extension,
            "reason": "guest appears angry or distressed",
            "transfer_type": "human",
        }
    if failed_intent_count >= 2:
        return {
            "action": "transfer",
            "extension": human_extension,
            "reason": "intent failed repeatedly",
            "transfer_type": "human",
        }
    return None


@dataclass
class CallSession:
    call_id: str
    caller_id: str = ""
    preferred_language: str = "en"
    detected_language: str = "en"
    history: list[dict[str, Any]] = field(default_factory=list)
    transfer_requested: bool = False
    transfer_reason: str | None = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    failed_intent_count: int = 0
    log_dir: Path = field(default_factory=lambda: Path(os.getenv("CALL_LOG_DIR", "/var/log/asterisk/ai/calls")))

    @classmethod
    def from_agi_env(cls, agi_env: dict[str, str], default_language: str = "en") -> "CallSession":
        raw_id = agi_env.get("agi_uniqueid") or str(uuid.uuid4())
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_id)
        caller_id = agi_env.get("agi_callerid") or agi_env.get("agi_calleridname") or "unknown"
        return cls(call_id=safe_id, caller_id=caller_id, preferred_language=default_language, detected_language=default_language)

    def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.updated_at = time.time()
        record = {"ts": self.updated_at, "type": event_type, **payload}
        self.history.append(record)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / f"{self.call_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def update_language(self, language: str, confidence: float = 1.0, source: str = "detector") -> None:
        if language in SUPPORTED_LANGUAGES and confidence >= 0.6:
            self.detected_language = language
            if language != self.preferred_language:
                self.preferred_language = language
                self.append_event("language_change", {"language": language, "confidence": confidence, "source": source})

    def request_transfer(self, reason: str) -> None:
        self.transfer_requested = True
        self.transfer_reason = reason
        self.append_event("transfer_requested", {"reason": reason})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["log_dir"] = str(self.log_dir)
        return data
