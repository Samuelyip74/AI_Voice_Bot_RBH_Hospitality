import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession, detect_language_from_text, language_response_instruction


def test_detects_mandarin_text():
    language, confidence = detect_language_from_text(
        "\u4f60\u597d\uff0c\u6211\u60f3\u67e5\u8be2\u8ba2\u5355",
        "en",
    )
    assert language == "zh"
    assert confidence >= 0.6


def test_detects_cantonese_text():
    language, confidence = detect_language_from_text(
        "\u5514\u8a72\uff0c\u6211\u60f3\u6435\u524d\u53f0",
        "en",
    )
    assert language == "zh-yue"
    assert confidence >= 0.6


def test_detects_japanese_text():
    language, confidence = detect_language_from_text(
        "\u3053\u3093\u306b\u3061\u306f\u3001\u4e88\u7d04\u3092\u78ba\u8a8d\u3057\u305f\u3044\u3067\u3059",
        "en",
    )
    assert language == "ja"
    assert confidence >= 0.6


def test_detects_arabic_text():
    language, confidence = detect_language_from_text(
        "\u0645\u0631\u062d\u0628\u0627\u060c \u0623\u0631\u064a\u062f \u0645\u0633\u0627\u0639\u062f\u0629",
        "en",
    )
    assert language == "ar"
    assert confidence >= 0.6


def test_detects_hindi_text():
    language, confidence = detect_language_from_text(
        "\u0928\u092e\u0938\u094d\u0924\u0947, \u092e\u0941\u091d\u0947 \u092e\u0926\u0926 \u091a\u093e\u0939\u093f\u090f",
        "en",
    )
    assert language == "hi"
    assert confidence >= 0.6


def test_detects_spanish_text():
    language, confidence = detect_language_from_text("Hola, necesito ayuda con mi reserva", "en")
    assert language == "es"
    assert confidence >= 0.6


def test_detects_french_text():
    language, confidence = detect_language_from_text("Bonjour, je voudrais une reservation", "en")
    assert language == "fr"
    assert confidence >= 0.6


def test_detects_vietnamese_text():
    language, confidence = detect_language_from_text(
        "Xin chao, toi can giup do",
        "en",
    )
    assert language == "vi"
    assert confidence >= 0.6


def test_detects_cantonese_hotel_cleaning_request():
    language, confidence = detect_language_from_text(
        "\u9ebb\u7169\u4f60\u5e6b\u6211\u57f7\u623f",
        "en",
    )
    assert language == "zh-yue"
    assert confidence >= 0.6


def test_detects_mandarin_hotel_cleaning_request():
    language, confidence = detect_language_from_text(
        "\u8bf7\u5e2e\u6211\u6253\u626b\u623f\u95f4",
        "en",
    )
    assert language == "zh"
    assert confidence >= 0.6


def test_mandarin_wakeup_request_does_not_switch_to_cantonese():
    language, confidence = detect_language_from_text(
        "\u5e2e\u6211\u5b89\u6392\u4e00\u4e2a\u53eb\u9192\u670d\u52a1\uff0c5\u670812\u53f7\u65e9\u4e0a7\u70b9\u3002",
        "zh",
    )
    assert language == "zh"
    assert confidence >= 0.8


def test_traditional_mandarin_frequency_answer_stays_in_chinese_family():
    language, confidence = detect_language_from_text(
        "\u6211\u9700\u8981\u4e00\u6b21\u6027\u7684\u53eb\u9192\u670d\u52d9\u3002",
        "zh-yue",
    )
    assert language in {"zh", "zh-yue"}
    assert confidence >= 0.8


def test_detects_malay_hotel_cleaning_request():
    language, confidence = detect_language_from_text("Tolong bersihkan bilik saya", "en")
    assert language == "ms"
    assert confidence >= 0.6


def test_detects_indonesian_hotel_cleaning_request():
    language, confidence = detect_language_from_text("Tolong bersihkan kamar saya", "en")
    assert language == "id"
    assert confidence >= 0.6


def test_detects_thai_room_service_request():
    language, confidence = detect_language_from_text(
        "\u0e02\u0e2d\u0e23\u0e39\u0e21\u0e40\u0e0b\u0e2d\u0e23\u0e4c\u0e27\u0e34\u0e2a\u0e44\u0e1b\u0e17\u0e35\u0e48\u0e2b\u0e49\u0e2d\u0e07",
        "en",
    )
    assert language == "th"
    assert confidence >= 0.6


def test_detects_french_front_desk_request():
    language, confidence = detect_language_from_text("Bonjour, je voudrais parler a la reception", "en")
    assert language == "fr"
    assert confidence >= 0.6


def test_detects_spanish_wake_up_call_request():
    language, confidence = detect_language_from_text("Necesito una llamada de despertador", "en")
    assert language == "es"
    assert confidence >= 0.6


def test_short_chinese_filler_does_not_switch_language():
    language, confidence = detect_language_from_text("\u55ef\u3002", "en")
    assert language == "en"
    assert confidence < 0.6


def test_spanish_accented_noise_does_not_switch_to_vietnamese():
    language, confidence = detect_language_from_text("razon.", "en")
    assert language == "en"
    assert confidence < 0.6


def test_single_ambiguous_halo_does_not_switch_language():
    language, confidence = detect_language_from_text("Halo,", "en")
    assert language == "en"
    assert confidence < 0.6


def test_short_hangul_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("\ud654\ud569\ud558\uac8c", "zh")
    assert language == "ko"
    assert confidence < 0.9


def test_clear_korean_sentence_reaches_switch_threshold():
    language, confidence = detect_language_from_text(
        "\uc548\ub155\ud558\uc138\uc694 \ub8f8\uc11c\ube44\uc2a4\ub97c \ubd80\ud0c1\ub4dc\ub9bd\ub2c8\ub2e4",
        "en",
    )
    assert language == "ko"
    assert confidence >= 0.9


def test_short_arabic_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("\u062e\u0644\u0649.", "zh")
    assert language == "ar"
    assert confidence < 0.9


def test_single_arabic_word_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("\u062c\u0645\u0647\u0648\u0631\u064a\u0629", "zh")
    assert language == "ar"
    assert confidence < 0.9


def test_clear_arabic_sentence_reaches_switch_threshold():
    language, confidence = detect_language_from_text(
        "\u0645\u0631\u062d\u0628\u0627 \u0623\u0631\u064a\u062f \u062e\u062f\u0645\u0629 \u0627\u0644\u063a\u0631\u0641 \u0645\u0646 \u0641\u0636\u0644\u0643",
        "en",
    )
    assert language == "ar"
    assert confidence >= 0.9


def test_short_japanese_noise_does_not_reach_switch_threshold():
    language, confidence = detect_language_from_text("\u306a\u305c\u3060\u3002", "en")
    assert language == "ja"
    assert confidence < 0.9


def test_clear_japanese_sentence_reaches_switch_threshold():
    language, confidence = detect_language_from_text(
        "\u3053\u3093\u306b\u3061\u306f\u3001\u30eb\u30fc\u30e0\u30b5\u30fc\u30d3\u30b9\u3092\u304a\u9858\u3044\u3057\u307e\u3059",
        "en",
    )
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


def test_explicit_english_request_switches_from_chinese():
    language, confidence = detect_language_from_text("Can you speak English?", "zh")
    assert language == "en"
    assert confidence >= 0.9


def test_english_sentence_switches_back_from_chinese():
    language, confidence = detect_language_from_text("I need a wake-up call tomorrow at 7 a.m.", "zh")
    assert language == "en"
    assert confidence >= 0.9
