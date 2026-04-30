import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import determine_transfer_action, should_transfer_deterministic
from openai_realtime_client import OpenAIRealtimeClient
from voice_assistant_eagi import normalize_transfer_extension


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


def test_room_service_routes_to_in_room_dining():
    action = determine_transfer_action("I want room service to my room")
    assert action["extension"] == "1921"
    assert action["transfer_type"] == "room_service"


def test_transfer_alias_front_desk_normalizes_to_1920():
    assert normalize_transfer_extension("front_desk", "human", "1920", "1921") == "1920"


def test_transfer_alias_room_service_normalizes_to_1921():
    assert normalize_transfer_extension("in_room_dining", "room_service", "1920", "1921") == "1921"


def test_parse_model_transfer_args(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    client = OpenAIRealtimeClient()
    parsed = client._parse_transfer_args('{"action":"transfer","extension":"1920","reason":"concierge help"}')
    assert parsed == {"action": "transfer", "extension": "1920", "reason": "concierge help"}
