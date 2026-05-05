import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import determine_transfer_action, should_end_call_deterministic, should_transfer_deterministic
from openai_realtime_client import OpenAIRealtimeClient
from voice_assistant_eagi import normalize_transfer_extension, service_request_confirmation_text, service_request_is_confirmed


def test_transfer_trigger_human_agent():
    transfer, reason = should_transfer_deterministic("Please get me a human agent")
    assert transfer is True
    assert "human" in reason


def test_transfer_trigger_operator():
    transfer, reason = should_transfer_deterministic("operator please")
    assert transfer is True
    assert reason


def test_hospitality_transfer_routes_to_concierge():
    action = determine_transfer_action("Please connect me to the front desk")
    assert action["extension"] == "1920"
    assert action["transfer_type"] == "human"


def test_room_service_request_does_not_transfer_by_default():
    action = determine_transfer_action("I want room service to my room")
    assert action is None


def test_explicit_room_service_team_request_transfers():
    action = determine_transfer_action("Please connect me to room service")
    assert action["extension"] == "1921"
    assert action["transfer_type"] == "room_service"


def test_transfer_alias_front_desk_normalizes_to_1920():
    assert normalize_transfer_extension("front_desk", "human", "1920", "1921") == "1920"


def test_transfer_alias_room_service_normalizes_to_1921():
    assert normalize_transfer_extension("in_room_dining", "room_service", "1920", "1921") == "1921"


def test_room_transfer_keeps_guest_room_number():
    assert normalize_transfer_extension("1208", "room", "1920", "1921") == "1208"


def test_parse_model_transfer_args(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeClient()
    parsed = client._parse_transfer_args('{"action":"transfer","extension":"1920","reason":"concierge help"}')
    assert parsed == {"action": "transfer", "extension": "1920", "reason": "concierge help"}


def test_parse_model_room_transfer_args(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeClient()
    parsed = client._parse_transfer_args(
        '{"action":"transfer","extension":"1208","transfer_type":"room","reason":"guest requested room transfer"}'
    )
    assert parsed == {
        "action": "transfer",
        "extension": "1208",
        "transfer_type": "room",
        "reason": "guest requested room transfer",
    }


def test_parse_service_request_args(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeClient()
    parsed = client._parse_service_request_args(
        '{"category":"housekeeping","summary":"Clean room 1208 this afternoon","room_number":"1208","priority":"normal","language":"en","confirmed_with_guest":true}'
    )
    assert parsed["category"] == "housekeeping"
    assert parsed["room_number"] == "1208"


def test_end_call_trigger_when_guest_has_no_more_requests():
    should_end, reason = should_end_call_deterministic("No thank you, that's all. Bye.")
    assert should_end is True
    assert reason


def test_parse_end_call_args(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeClient()
    parsed = client._parse_end_call_args('{"action":"end_call","reason":"guest said goodbye"}')
    assert parsed == {"action": "end_call", "reason": "guest said goodbye"}


def test_service_request_requires_explicit_confirmation():
    assert service_request_is_confirmed({"confirmed_with_guest": True}) is True
    assert service_request_is_confirmed({"confirmed_with_guest": False}) is False
    assert service_request_is_confirmed({}) is False


def test_service_request_confirmation_text_includes_summary_and_room():
    text = service_request_confirmation_text(
        {"summary": "spaghetti aglio e olio", "room_number": "1002"},
        "en",
    )
    assert "Please confirm" in text
    assert "spaghetti aglio e olio" in text
    assert "1002" in text
