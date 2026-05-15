import base64
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from openai_realtime_translation_client import (
    OpenAIRealtimeTranslationClient,
    detect_translation_request,
    normalize_translation_language,
)


def test_detect_translation_request_default_target():
    action = detect_translation_request("Can you translate for me?", "en")

    assert action["action"] == "start_translation"
    assert action["target_language"] == "en"
    assert action["target_language_name"] == "English"


def test_detect_translation_request_extracts_target_language():
    action = detect_translation_request("Please start live translation into Japanese.", "en")

    assert action["target_language"] == "ja"
    assert action["target_language_name"] == "Japanese"


def test_detect_translation_request_ignores_regular_concierge_requests():
    assert detect_translation_request("Please send housekeeping to room 1208.", "en") is None


def test_normalize_translation_language_aliases():
    assert normalize_translation_language("Mandarin") == "zh"
    assert normalize_translation_language("cantonese") == "zh-yue"
    assert normalize_translation_language("unknown", default="ms") == "ms"


def test_translation_session_update_payload(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeTranslationClient()

    payload = client.session_update_payload("ja")

    assert payload["type"] == "session.update"
    assert payload["session"]["audio"]["output"]["language"] == "ja"
    assert "format" not in payload["session"]["audio"]["input"]
    assert payload["session"]["audio"]["input"]["transcription"]["model"] == "gpt-realtime-whisper"


def test_translation_event_handler_accumulates_audio_and_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeTranslationClient()

    from openai_realtime_translation_client import TranslationTurnResult

    turn = TranslationTurnResult()
    client._handle_event({"type": "translation.text.delta", "delta": "Hello"}, turn)
    client._handle_event({"type": "translation.text.delta", "delta": " there"}, turn)
    client._handle_event(
        {
            "type": "translation.audio.delta",
            "delta": base64.b64encode(b"pcm").decode("ascii"),
        },
        turn,
    )

    assert turn.translated_text == "Hello there"
    assert turn.translated_audio_pcm24k == b"pcm"
