import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession, determine_transfer_action, should_end_call_deterministic, should_transfer_deterministic
from openai_realtime_client import OpenAIRealtimeClient
from voice_assistant_eagi import (
    model_transfer_action_is_allowed,
    normalize_transfer_extension,
    notify_rainbow_service_request,
    rainbow_service_request_destination,
    service_request_confirmation_text,
    service_request_is_confirmed,
    submit_service_request_notifications,
    transcript_should_be_ignored,
)


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


def test_model_human_transfer_suppressed_for_food_order_request():
    allowed, reason = model_transfer_action_is_allowed(
        "I'd like to order some food.",
        {"action": "transfer", "extension": "1920", "transfer_type": "human", "reason": "guest requested concierge"},
        "1920",
        "1921",
    )

    assert allowed is False
    assert "explicitly request" in reason


def test_model_room_service_transfer_suppressed_for_food_order_request():
    allowed, reason = model_transfer_action_is_allowed(
        "I'd like to order some food.",
        {"action": "transfer", "extension": "1921", "transfer_type": "room_service", "reason": "food order"},
        "1920",
        "1921",
    )

    assert allowed is False
    assert "not an explicit request" in reason


def test_model_human_transfer_allowed_for_front_desk_request():
    allowed, reason = model_transfer_action_is_allowed(
        "Please connect me to the front desk.",
        {"action": "transfer", "extension": "1920", "transfer_type": "human", "reason": "front desk"},
        "1920",
        "1921",
    )

    assert allowed is True
    assert reason is None


def test_model_room_service_transfer_allowed_for_explicit_room_service_staff_request():
    allowed, reason = model_transfer_action_is_allowed(
        "Please connect me to room service.",
        {"action": "transfer", "extension": "1921", "transfer_type": "room_service", "reason": "room service staff"},
        "1920",
        "1921",
    )

    assert allowed is True
    assert reason is None


def test_model_room_transfer_allowed_for_direct_room_request():
    allowed, reason = model_transfer_action_is_allowed(
        "Can you connect me to room 1208?",
        {"action": "transfer", "extension": "1208", "transfer_type": "room", "reason": "direct room transfer"},
        "1920",
        "1921",
    )

    assert allowed is True
    assert reason is None


def test_short_low_confidence_foreign_transcript_is_ignored():
    ignored, reason = transcript_should_be_ignored("見た。", "ja", 0.82, "en")
    assert ignored is True
    assert "low-confidence foreign transcript" in reason


def test_clear_language_switch_transcript_is_not_ignored():
    ignored, reason = transcript_should_be_ignored("こんにちは、ルームサービスをお願いします", "ja", 0.92, "en")
    assert ignored is False
    assert reason is None


def test_normal_english_transcript_is_not_ignored():
    ignored, reason = transcript_should_be_ignored("I would like to order some food.", "en", 0.55, "en")
    assert ignored is False
    assert reason is None


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


def test_rainbow_room_service_destination_uses_room_service_bubble(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination({"category": "room_service"})

    assert destination == "room_service"
    assert jid == "room-service@conference.openrainbow.com"


def test_rainbow_non_room_service_destination_uses_front_desk_bubble(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination({"category": "housekeeping"})

    assert destination == "front_desk"
    assert jid == "front-desk@conference.openrainbow.com"


def test_rainbow_notification_skips_when_bubble_missing(monkeypatch):
    monkeypatch.setenv("RAINBOW_NODE_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.delenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", raising=False)
    session = CallSession(call_id="rainbow-test", caller_id="1000")

    result = notify_rainbow_service_request(session, {"category": "room_service", "summary": "Tea"})

    assert result["sent"] is False
    assert "RAINBOW_ROOM_SERVICE_BUBBLE_JID" in result["reason"]


def test_service_request_notifications_queue_rainbow_without_blocking(monkeypatch, tmp_path):
    monkeypatch.setenv("CALL_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("HOTEL_REQUEST_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("RAINBOW_NODE_ASYNC", "true")
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    session = CallSession(call_id="async-rainbow-test", caller_id="1000", log_dir=tmp_path)

    webhook_result, rainbow_result = asyncio.run(
        submit_service_request_notifications(session, {"category": "room_service", "summary": "Tea"})
    )

    assert webhook_result["sent"] is False
    assert rainbow_result["queued"] is True
    assert rainbow_result["destination"] == "room_service"
