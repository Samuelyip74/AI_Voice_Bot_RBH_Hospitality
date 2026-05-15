from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any


LOGGER = logging.getLogger(__name__)


REALTIME_TRANSLATION_URL = "wss://api.openai.com/v1/realtime/translations"


SUPPORTED_TRANSLATION_LANGUAGES = {
    "en": "English",
    "zh": "Chinese",
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


TRANSLATION_REQUEST_PATTERNS = [
    r"\blive (translation|translate|translator|interpreter|interpretation)\b",
    r"\breal[- ]?time (translation|translate|translator|interpreter|interpretation)\b",
    r"\btranslate (for me|this call|between us|between me and)\b",
    r"\binterpret (for me|this call|between us|between me and)\b",
    r"\bcan you (translate|interpret)\b",
    r"\bi need (a )?(translator|interpreter)\b",
    r"\bhelp me (translate|interpret)\b",
    r"(实时|即時|现场|現場|翻译|翻譯|传译|傳譯|口译|口譯|同声传译|同聲傳譯)",
    r"(terjemah|penterjemah|jurubahasa)",
    r"(மொழிபெயர்|மொழிபெயர்ப்பு)",
]


LANGUAGE_TARGET_PATTERNS = [
    ("en", r"\b(to|into|in)\s+english\b|英文|英语|英語|bahasa inggeris|ஆங்கிலம்"),
    ("zh", r"\b(to|into|in)\s+(mandarin|chinese)\b|中文|普通话|普通話|华语|華語"),
    ("zh-yue", r"\b(to|into|in)\s+cantonese\b|广东话|廣東話|粤语|粵語"),
    ("ms", r"\b(to|into|in)\s+malay\b|bahasa melayu"),
    ("ta", r"\b(to|into|in)\s+tamil\b|தமிழ்"),
    ("ja", r"\b(to|into|in)\s+japanese\b|日本語"),
    ("ko", r"\b(to|into|in)\s+korean\b|한국어"),
    ("th", r"\b(to|into|in)\s+thai\b|ภาษาไทย"),
    ("vi", r"\b(to|into|in)\s+vietnamese\b|tiếng việt"),
    ("id", r"\b(to|into|in)\s+indonesian\b|bahasa indonesia"),
    ("fr", r"\b(to|into|in)\s+french\b|français"),
    ("es", r"\b(to|into|in)\s+spanish\b|español"),
    ("de", r"\b(to|into|in)\s+german\b|deutsch"),
    ("it", r"\b(to|into|in)\s+italian\b|italiano"),
    ("ar", r"\b(to|into|in)\s+arabic\b|العربية"),
    ("hi", r"\b(to|into|in)\s+hindi\b|हिन्दी|हिंदी"),
]


def normalize_translation_language(value: str | None, default: str = "en") -> str:
    normalized = (value or "").strip().lower().replace("_", "-")
    aliases = {
        "english": "en",
        "mandarin": "zh",
        "chinese": "zh",
        "cantonese": "zh-yue",
        "yue": "zh-yue",
        "malay": "ms",
        "tamil": "ta",
        "japanese": "ja",
        "korean": "ko",
        "thai": "th",
        "vietnamese": "vi",
        "indonesian": "id",
        "french": "fr",
        "spanish": "es",
        "german": "de",
        "italian": "it",
        "arabic": "ar",
        "hindi": "hi",
    }
    code = aliases.get(normalized, normalized or default)
    return code if code in SUPPORTED_TRANSLATION_LANGUAGES else default


def detect_translation_request(text: str, default_target_language: str = "en") -> dict[str, str] | None:
    normalized = (text or "").strip().lower()
    if not normalized:
        return None
    if not any(re.search(pattern, normalized, re.IGNORECASE) for pattern in TRANSLATION_REQUEST_PATTERNS):
        return None
    target_language = normalize_translation_language(default_target_language)
    for code, pattern in LANGUAGE_TARGET_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            target_language = code
            break
    return {
        "action": "start_translation",
        "target_language": target_language,
        "target_language_name": SUPPORTED_TRANSLATION_LANGUAGES[target_language],
        "reason": "guest requested live translation",
    }


@dataclass
class TranslationTurnResult:
    source_transcript: str = ""
    translated_text: str = ""
    translated_audio_pcm24k: bytes = b""
    error: str | None = None


class OpenAIRealtimeTranslationClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_REALTIME_TRANSLATION_MODEL", "gpt-realtime-translate")
        self.timeout_seconds = timeout_seconds or float(os.getenv("REALTIME_TRANSLATION_TIMEOUT_SECONDS", "45"))
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

    def session_update_payload(self, target_language: str, source_rate: int = 24000) -> dict[str, Any]:
        language_code = normalize_translation_language(target_language)
        return {
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "transcription": {"model": "gpt-realtime-whisper"},
                        "noise_reduction": {"type": "near_field"},
                    },
                    "output": {"language": language_code},
                },
            },
        }

    async def translate_utterance(self, pcm24k: bytes, target_language: str = "en") -> TranslationTurnResult:
        return await asyncio.wait_for(
            self._translate_utterance_ws(pcm24k, target_language),
            timeout=self.timeout_seconds,
        )

    async def _translate_utterance_ws(self, pcm24k: bytes, target_language: str) -> TranslationTurnResult:
        import websockets

        result = TranslationTurnResult()
        url = f"{REALTIME_TRANSLATION_URL}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with websockets.connect(url, additional_headers=headers, max_size=20 * 1024 * 1024) as ws:
            await ws.send(json.dumps(self.session_update_payload(target_language)))
            await ws.send(
                json.dumps(
                    {
                        "type": "session.input_audio_buffer.append",
                        "audio": base64.b64encode(pcm24k).decode("ascii"),
                    }
                )
            )

            while True:
                event = json.loads(await ws.recv())
                event_type = self._handle_event(event, result)
                if event_type in {"translation.done", "response.done"}:
                    break
                if event_type == "error":
                    break
        return result

    def _handle_event(self, event: dict[str, Any], result: TranslationTurnResult) -> str:
        event_type = event.get("type", "")
        LOGGER.debug("Realtime translation event: %s", event_type)

        if event_type in {
            "conversation.item.input_audio_transcription.completed",
            "translation.input_audio_transcription.completed",
            "session.input_audio_transcription.completed",
        }:
            result.source_transcript = event.get("transcript", "")
        elif event_type in {
            "translation.text.delta",
            "translation.transcript.delta",
            "session.output_text.delta",
            "session.output_transcript.delta",
            "session.output_audio_transcript.delta",
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
        }:
            result.translated_text += event.get("delta", "")
        elif event_type in {
            "translation.audio.delta",
            "session.output_audio.delta",
            "response.audio.delta",
            "response.output_audio.delta",
        }:
            result.translated_audio_pcm24k += base64.b64decode(event.get("delta", ""))
        elif event_type == "error":
            result.error = json.dumps(event.get("error", event), ensure_ascii=False)
        elif event_type in {"translation.done", "response.done"}:
            response = event.get("response") or event.get("translation") or {}
            status = response.get("status") if isinstance(response, dict) else None
            if status and status != "completed":
                result.error = json.dumps(response, ensure_ascii=False)
        return event_type
