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
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "ar": "Arabic",
    "hi": "Hindi",
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
    "fr": "Respond only in French.",
    "es": "Respond only in Spanish.",
    "de": "Respond only in German.",
    "it": "Respond only in Italian.",
    "ar": "Respond only in Arabic.",
    "hi": "Respond only in Hindi.",
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

ROOM_SERVICE_TRANSFER_PATTERNS = [
    r"\bconnect me to (room service|in-room dining)\b",
    r"\btransfer me to (room service|in-room dining)\b",
    r"\bspeak to (room service|in-room dining)\b",
    r"\btalk to (room service|in-room dining)\b",
    r"\broom service (agent|staff|team|person)\b",
    r"\bin-room dining (agent|staff|team|person)\b",
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

END_CALL_PATTERNS = [
    r"\bthat'?s all\b",
    r"\bnothing else\b",
    r"\bno more\b",
    r"\bno thanks\b",
    r"\bno thank you\b",
    r"\bi'?m done\b",
    r"\bthat will be all\b",
    r"\bthank you,? bye\b",
    r"\bthanks,? bye\b",
    r"\bgoodbye\b",
    r"\bbye\b",
]

LANGUAGE_HINTS = [
    (
        "zh-yue",
        [
            "唔", "咩", "係", "冇", "嘅", "喺", "佢", "哋", "而家", "幫我", "麻煩你",
            "廣東話", "粤语", "粵語", "執房", "攞", "搵", "前台", "房口", "叫醒",
        ],
    ),
    (
        "zh",
        [
            "你好", "谢谢", "謝謝", "请", "請", "中文", "普通话", "普通話", "吗", "嗎",
            "帮我", "幫我", "前台", "客房服务", "客房服務", "打扫房间", "打掃房間",
            "叫醒服务", "叫醒服務", "预订", "預訂", "出租车", "計程車", "餐厅", "餐廳",
        ],
    ),
    (
        "ms",
        [
            "bahasa melayu", "apa khabar", "saya mahu", "saya nak", "boleh bantu",
            "boleh tolong", "tolong bersihkan bilik", "bilik saya", "servis bilik",
            "makanan ke bilik", "panggilan bangun", "meja depan", "kaunter penyambut tetamu",
            "tempahan", "teksi", "sarapan", "terima kasih",
        ],
    ),
    (
        "ta",
        [
            "வணக்கம்", "நன்றி", "தமிழ்", "தயவு செய்து", "உதவி", "அறை", "என் அறை",
            "அறை சேவை", "சுத்தம்", "எழுப்பும் அழைப்பு", "முன் அலுவலகம்", "முன்பதிவு",
            "டாக்ஸி", "உணவு", "காலை உணவு",
        ],
    ),
    (
        "ja",
        [
            "こんにちは", "ありがとう", "日本語", "お願いします", "予約", "部屋", "客室",
            "ルームサービス", "清掃", "掃除", "起こしてください", "モーニングコール",
            "フロント", "タクシー", "朝食", "レストラン",
        ],
    ),
    (
        "ko",
        [
            "안녕하세요", "감사합니다", "한국어", "주세요", "예약", "방", "객실",
            "룸서비스", "청소", "깨워", "모닝콜", "프런트", "택시", "아침", "식당",
        ],
    ),
    (
        "th",
        [
            "สวัสดี", "ขอบคุณ", "ภาษาไทย", "กรุณา", "ช่วย", "ห้อง", "รูมเซอร์วิส",
            "ทำความสะอาด", "ปลุก", "แผนกต้อนรับ", "จอง", "แท็กซี่", "อาหารเช้า", "ร้านอาหาร",
        ],
    ),
    (
        "vi",
        [
            "xin chào", "cảm ơn", "tiếng việt", "vui lòng", "làm ơn", "giúp tôi",
            "phòng của tôi", "dịch vụ phòng", "dọn phòng", "báo thức", "lễ tân",
            "đặt phòng", "đặt bàn", "taxi", "bữa sáng", "nhà hàng",
        ],
    ),
    (
        "id",
        [
            "bahasa indonesia", "saya ingin", "saya mau", "tolong bantu",
            "tolong bersihkan kamar", "kamar saya", "layanan kamar", "makanan ke kamar",
            "panggilan bangun", "resepsionis", "meja depan", "pemesanan", "taksi",
            "sarapan", "terima kasih",
        ],
    ),
    (
        "fr",
        [
            "bonjour", "merci", "s'il vous plaît", "je voudrais", "pouvez-vous",
            "ma chambre", "service d'étage", "nettoyer la chambre", "réveil", "réception",
            "réserver", "taxi", "petit déjeuner", "restaurant",
        ],
    ),
    (
        "es",
        [
            "hola", "gracias", "por favor", "quisiera", "puede ayudarme", "mi habitación",
            "servicio a la habitación", "servicio de habitaciones", "limpiar la habitación",
            "llamada de despertador", "recepción", "reservar", "taxi", "desayuno", "restaurante",
        ],
    ),
    (
        "de",
        [
            "guten tag", "hallo", "danke", "bitte", "ich möchte", "können sie",
            "mein zimmer", "zimmerservice", "zimmer reinigen", "weckruf", "rezeption",
            "reservieren", "taxi", "frühstück", "restaurant",
        ],
    ),
    (
        "it",
        [
            "buongiorno", "grazie", "per favore", "vorrei", "può aiutarmi", "la mia camera",
            "servizio in camera", "pulire la camera", "sveglia", "reception", "prenotare",
            "taxi", "colazione", "ristorante",
        ],
    ),
    (
        "ar",
        [
            "مرحبا", "شكرا", "من فضلك", "أريد", "العربية", "ساعدني", "غرفتي",
            "خدمة الغرف", "تنظيف الغرفة", "مكالمة إيقاظ", "الاستقبال", "حجز",
            "سيارة أجرة", "فطور", "مطعم",
        ],
    ),
    (
        "hi",
        [
            "नमस्ते", "धन्यवाद", "कृपया", "मदद", "हिन्दी", "हिंदी", "मेरा कमरा",
            "रूम सर्विस", "कमरा साफ", "वेक अप कॉल", "रिसेप्शन", "बुकिंग", "टैक्सी",
            "नाश्ता", "रेस्तरां",
        ],
    ),
]

LATIN_LANGUAGE_MARKERS = {
    "ms": {
        "saya", "mahu", "nak", "boleh", "bilik", "makan", "tolong", "terima", "kasih",
        "bersihkan", "sarapan", "teksi", "kaunter", "tempahan", "bangun",
    },
    "id": {
        "saya", "ingin", "mau", "kamar", "makanan", "tolong", "bahasa", "indonesia",
        "bersihkan", "sarapan", "taksi", "resepsionis", "pemesanan", "bangun",
    },
    "vi": {
        "xin", "chào", "cảm", "ơn", "vui", "lòng", "phòng", "tiếng", "việt",
        "dọn", "báo", "thức", "lễ", "tân", "đặt", "bữa", "sáng",
    },
    "fr": {
        "bonjour", "merci", "voudrais", "chambre", "réservation", "s'il", "plaît",
        "réception", "réveil", "nettoyer", "taxi", "petit", "déjeuner",
    },
    "es": {
        "hola", "gracias", "favor", "quisiera", "habitación", "reserva", "ayuda",
        "recepción", "despertador", "limpiar", "taxi", "desayuno",
    },
    "de": {
        "guten", "hallo", "danke", "bitte", "möchte", "zimmer", "reservierung", "hilfe",
        "rezeption", "weckruf", "reinigen", "taxi", "frühstück",
    },
    "it": {
        "buongiorno", "grazie", "favore", "vorrei", "camera", "prenotazione", "aiuto",
        "reception", "sveglia", "pulire", "taxi", "colazione",
    },
    "en": {
        "hello", "hi", "please", "thanks", "room", "reservation", "help", "front", "desk",
        "housekeeping", "cleaning", "wake", "breakfast", "restaurant", "taxi",
    },
}

SCRIPT_PATTERNS = {
    "ta": re.compile(r"[\u0b80-\u0bff]"),
    "ja": re.compile(r"[\u3040-\u30ff]"),
    "ko": re.compile(r"[\uac00-\ud7af]"),
    "th": re.compile(r"[\u0e00-\u0e7f]"),
    "ar": re.compile(r"[\u0600-\u06ff]"),
    "hi": re.compile(r"[\u0900-\u097f]"),
}

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CANTONESE_PATTERN = re.compile(r"[唔咩係冇嘅喺佢哋]|而家|廣東話|粤语|粵語|執房|攞|搵|房口")
VIETNAMESE_DIACRITIC_PATTERN = re.compile(
    r"[ăâêôơưđáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
    re.IGNORECASE,
)
VIETNAMESE_STRONG_DIACRITIC_PATTERN = re.compile(r"[ăơưđắằẳẵặớờởỡợứừửữự]", re.IGNORECASE)

LOW_INFORMATION_UTTERANCES = {
    "um",
    "uh",
    "er",
    "ah",
    "hmm",
    "mm",
    "mhm",
    "嗯",
    "嗯嗯",
    "啊",
    "哦",
    "喂",
    "唔",
    "はい",
    "ええ",
    "네",
    "예",
}

AMBIGUOUS_SINGLE_WORDS = {
    "hello",
    "hi",
    "hey",
    "halo",
    "hallo",
    "hola",
    "alo",
    "bonjour",
    "razon",
    "razón",
    "okay",
    "ok",
    "yes",
    "no",
}


def detect_language_from_text(text: str, default: str = "en") -> tuple[str, float]:
    """Deterministic language detector used as a guardrail around model behavior."""
    normalized = text.strip().lower()
    if not normalized:
        return default, 0.0
    normalized_plain = re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", "", normalized, flags=re.UNICODE)
    words_for_guard = re.findall(r"[a-zA-ZÀ-ÿ']+", normalized)
    cjk_chars = CJK_PATTERN.findall(text)

    if normalized_plain in LOW_INFORMATION_UTTERANCES:
        return default, 0.2
    if len(cjk_chars) == 1 and len(normalized_plain) <= 1:
        return default, 0.25
    if len(words_for_guard) == 1 and words_for_guard[0].lower() in AMBIGUOUS_SINGLE_WORDS:
        return default, 0.35

    for code, hints in LANGUAGE_HINTS:
        if any(hint.lower() in normalized for hint in hints):
            return code, 0.92

    for code, pattern in SCRIPT_PATTERNS.items():
        if pattern.search(text):
            return code, 0.9

    if CJK_PATTERN.search(text):
        if CANTONESE_PATTERN.search(text):
            return "zh-yue", 0.86
        return "zh", 0.78

    if VIETNAMESE_STRONG_DIACRITIC_PATTERN.search(text):
        return "vi", 0.84

    words = set(re.findall(r"[a-zA-ZÀ-ÿ']+", normalized))
    if words:
        scored = sorted(
            ((len(words & markers), code) for code, markers in LATIN_LANGUAGE_MARKERS.items()),
            reverse=True,
        )
        best_score, best_code = scored[0]
        if best_score >= 2:
            return best_code, min(0.92, 0.62 + best_score * 0.08)
        if best_score == 1 and best_code != "en":
            return best_code, 0.62

    return default, 0.55


def language_response_instruction(language: str) -> str:
    return LANGUAGE_RESPONSE_INSTRUCTIONS.get(language, LANGUAGE_RESPONSE_INSTRUCTIONS["en"])


def should_transfer_deterministic(text: str, failed_intent_count: int = 0, angry: bool = False) -> tuple[bool, str | None]:
    action = determine_transfer_action(text, failed_intent_count=failed_intent_count, angry=angry)
    if action:
        return True, action["reason"]
    return False, None


def should_end_call_deterministic(text: str) -> tuple[bool, str | None]:
    normalized = text.lower()
    for pattern in END_CALL_PATTERNS:
        if re.search(pattern, normalized):
            return True, "guest indicated there are no more requests"
    return False, None


def determine_transfer_action(
    text: str,
    failed_intent_count: int = 0,
    angry: bool = False,
    human_extension: str = "1920",
    room_service_extension: str = "1921",
) -> dict[str, str] | None:
    normalized = text.lower()
    for pattern in ROOM_SERVICE_TRANSFER_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "action": "transfer",
                "extension": room_service_extension,
                "reason": "guest explicitly requested room service or in-room dining staff",
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
        if language not in SUPPORTED_LANGUAGES:
            return

        switch_threshold = float(os.getenv("LANGUAGE_SWITCH_CONFIDENCE", "0.90"))
        if language == self.preferred_language and confidence >= 0.6:
            self.detected_language = language
            return

        if confidence >= switch_threshold:
            self.detected_language = language
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
