import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession
from voice_assistant_eagi import (
    agi_response_is_dead_channel,
    agi_result_code,
    agi_variable_value,
    build_greeting_text,
    greeting_caller_name,
    parse_sip_from_header,
    transfer_reclaim_enabled,
)


def test_agi_result_code_parses_success():
    assert agi_result_code("200 result=6") == 6


def test_agi_result_code_parses_failure_with_text():
    assert agi_result_code("200 result=1 (FAILURE)") == 1


def test_dead_channel_response_detected():
    assert agi_response_is_dead_channel("511 Command Not Permitted on a dead channel or intercept routine")
    assert agi_response_is_dead_channel(
        "511 Command Not Permitted on a dead channel or intercept routine | "
        "511 Command Not Permitted on a dead channel or intercept routine"
    )


def test_agi_variable_value_parses_parenthesized_value():
    assert agi_variable_value("200 result=1 (Samuel Yip - 1910 - EN)") == "Samuel Yip - 1910 - EN"


def test_parse_sip_from_header_extracts_display_name_and_user():
    parsed = parse_sip_from_header(
        '"Samuel Yip - 1910 - EN" <sip:99902200110783813606957949@313.apac1.sip.openrainbow.com>;tag=abc'
    )

    assert parsed["display_name"] == "Samuel Yip - 1910 - EN"
    assert parsed["sip_user"] == "99902200110783813606957949"


def test_greeting_caller_name_removes_routing_suffix():
    assert greeting_caller_name("Samuel Yip - 1910 - EN") == "Samuel Yip"


def test_build_greeting_text_personalizes_with_caller_name(monkeypatch):
    monkeypatch.setenv("AI_GREETING_PERSONALIZE", "true")
    monkeypatch.setenv("AI_GREETING_PERSONAL_TEXT", "Hello {caller_name}, how may I help?")
    session = CallSession(call_id="greeting-test", caller_name="Samuel Yip - 1910 - EN")

    text, personalized = build_greeting_text(session)

    assert text == "Hello Samuel Yip, how may I help?"
    assert personalized is True


def test_transfer_reclaim_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("TRANSFER_RECLAIM_ON_FAILURE", raising=False)

    assert transfer_reclaim_enabled() is True


def test_transfer_reclaim_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TRANSFER_RECLAIM_ON_FAILURE", "false")

    assert transfer_reclaim_enabled() is False
