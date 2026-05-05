import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession, detect_language_from_text, language_response_instruction


def test_detects_mandarin_text():
    language, confidence = detect_language_from_text("你好，我想查询订单", "en")
    assert language == "zh"
    assert confidence >= 0.6


def test_detects_cantonese_text():
    language, confidence = detect_language_from_text("唔該，我想搵前台", "en")
    assert language == "zh-yue"
    assert confidence >= 0.6


def test_detects_japanese_text():
    language, confidence = detect_language_from_text("こんにちは、予約を確認したいです", "en")
    assert language == "ja"
    assert confidence >= 0.6


def test_detects_arabic_text():
    language, confidence = detect_language_from_text("مرحبا، أريد مساعدة", "en")
    assert language == "ar"
    assert confidence >= 0.6


def test_detects_hindi_text():
    language, confidence = detect_language_from_text("नमस्ते, मुझे मदद चाहिए", "en")
    assert language == "hi"
    assert confidence >= 0.6


def test_detects_spanish_text():
    language, confidence = detect_language_from_text("Hola, necesito ayuda con mi reserva", "en")
    assert language == "es"
    assert confidence >= 0.6


def test_detects_french_text():
    language, confidence = detect_language_from_text("Bonjour, je voudrais une réservation", "en")
    assert language == "fr"
    assert confidence >= 0.6


def test_detects_vietnamese_text():
    language, confidence = detect_language_from_text("Xin chào, tôi cần giúp đỡ", "en")
    assert language == "vi"
    assert confidence >= 0.6


def test_detects_cantonese_hotel_cleaning_request():
    language, confidence = detect_language_from_text("麻煩你幫我執房", "en")
    assert language == "zh-yue"
    assert confidence >= 0.6


def test_detects_mandarin_hotel_cleaning_request():
    language, confidence = detect_language_from_text("请帮我打扫房间", "en")
    assert language == "zh"
    assert confidence >= 0.6


def test_detects_malay_hotel_cleaning_request():
    language, confidence = detect_language_from_text("Tolong bersihkan bilik saya", "en")
    assert language == "ms"
    assert confidence >= 0.6


def test_detects_indonesian_hotel_cleaning_request():
    language, confidence = detect_language_from_text("Tolong bersihkan kamar saya", "en")
    assert language == "id"
    assert confidence >= 0.6


def test_detects_thai_room_service_request():
    language, confidence = detect_language_from_text("ขอรูมเซอร์วิสไปที่ห้อง", "en")
    assert language == "th"
    assert confidence >= 0.6


def test_detects_french_front_desk_request():
    language, confidence = detect_language_from_text("Bonjour, je voudrais parler à la réception", "en")
    assert language == "fr"
    assert confidence >= 0.6


def test_detects_spanish_wake_up_call_request():
    language, confidence = detect_language_from_text("Necesito una llamada de despertador", "en")
    assert language == "es"
    assert confidence >= 0.6


def test_short_chinese_filler_does_not_switch_language():
    language, confidence = detect_language_from_text("嗯。", "en")
    assert language == "en"
    assert confidence < 0.6


def test_spanish_accented_noise_does_not_switch_to_vietnamese():
    language, confidence = detect_language_from_text("razón.", "en")
    assert language == "en"
    assert confidence < 0.6


def test_single_ambiguous_halo_does_not_switch_language():
    language, confidence = detect_language_from_text("Halo,", "en")
    assert language == "en"
    assert confidence < 0.6


def test_short_hangul_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("화합하게", "zh")
    assert language == "ko"
    assert confidence < 0.9


def test_clear_korean_sentence_reaches_switch_threshold():
    language, confidence = detect_language_from_text("안녕하세요 룸서비스를 부탁드립니다", "en")
    assert language == "ko"
    assert confidence >= 0.9


def test_short_arabic_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("خلى.", "zh")
    assert language == "ar"
    assert confidence < 0.9


def test_single_arabic_word_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("جمهورية", "zh")
    assert language == "ar"
    assert confidence < 0.9


def test_clear_arabic_sentence_reaches_switch_threshold():
    language, confidence = detect_language_from_text("مرحبا أريد خدمة الغرف من فضلك", "en")
    assert language == "ar"
    assert confidence >= 0.9


def test_short_japanese_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("なぜだ。", "en")
    assert language == "ja"
    assert confidence < 0.9


def test_clear_japanese_sentence_reaches_switch_threshold():
    language, confidence = detect_language_from_text("こんにちは、ルームサービスをお願いします", "en")
    assert language == "ja"
    assert confidence >= 0.9


def test_session_updates_preferred_language():
    session = CallSession(call_id="test")
    session.log_dir = Path("logs/test-calls")
    session.update_language("vi", 0.9)
    assert session.preferred_language == "vi"


def test_session_does_not_switch_language_below_threshold(monkeypatch):
    monkeypatch.setenv("LANGUAGE_SWITCH_CONFIDENCE", "0.90")
    session = CallSession(call_id="test")
    session.log_dir = Path("logs/test-calls")
    session.update_language("vi", 0.89)
    assert session.preferred_language == "en"


def test_language_response_instruction_for_mandarin():
    assert "Mandarin Chinese" in language_response_instruction("zh")
