import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession, detect_language_from_text, language_response_instruction


def test_detects_mandarin_text():
    language, confidence = detect_language_from_text("你好，我想查询订单", "en")
    assert language == "zh"
    assert confidence >= 0.6


def test_detects_japanese_text():
    language, confidence = detect_language_from_text("こんにちは、予約を確認したいです", "en")
    assert language == "ja"
    assert confidence >= 0.6


def test_session_updates_preferred_language():
    session = CallSession(call_id="test")
    session.log_dir = Path("logs/test-calls")
    session.update_language("vi", 0.9)
    assert session.preferred_language == "vi"


def test_language_response_instruction_for_mandarin():
    assert "Mandarin Chinese" in language_response_instruction("zh")
