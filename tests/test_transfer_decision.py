import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession, determine_transfer_action, should_end_call_deterministic, should_transfer_deterministic
from openai_realtime_client import OpenAIRealtimeClient
from voice_assistant_eagi import (
    normalize_transfer_extension,
    normalize_wakeup_frequency,
    notify_rainbow_service_request,
    post_wakeup_call_request,
    rainbow_service_request_destination,
    service_request_already_submitted,
    service_request_can_be_submitted,
    service_request_confirmation_text,
    service_request_fingerprint,
    service_request_is_confirmed,
    latest_unsubmitted_pending_service_request,
    submit_service_request_notifications,
    transcript_confirms_service_request,
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


def test_chinese_front_desk_transfer_routes_to_concierge():
    action = determine_transfer_action("\u4f60\u53ef\u4ee5\u5e6b\u6211\u8f49\u63a5\u53bb\u524d\u53f0\u55ce?")
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


def test_short_low_confidence_foreign_transcript_is_ignored():
    ignored, reason = transcript_should_be_ignored("\u898b\u305f\u3002", "ja", 0.82, "en")
    assert ignored is True
    assert "low-confidence foreign transcript" in reason


def test_clear_language_switch_transcript_is_not_ignored():
    ignored, reason = transcript_should_be_ignored(
        "\u3053\u3093\u306b\u3061\u306f\u3001\u30eb\u30fc\u30e0\u30b5\u30fc\u30d3\u30b9\u3092\u304a\u9858\u3044\u3057\u307e\u3059",
        "ja",
        0.92,
        "en",
    )
    assert ignored is False
    assert reason is None


def test_normal_english_transcript_is_not_ignored():
    ignored, reason = transcript_should_be_ignored("I would like to order some food.", "en", 0.55, "en")
    assert ignored is False
    assert reason is None


def test_same_language_chinese_room_number_transcript_is_not_ignored():
    ignored, reason = transcript_should_be_ignored("\u6211\u7684\u623f\u9593\u662f\u4e00\u96f6\u96f6\u4e00\u3002", "zh", 0.78, "zh")
    assert ignored is False
    assert reason is None


def test_chinese_family_transcript_is_not_ignored_across_mandarin_and_cantonese():
    ignored, reason = transcript_should_be_ignored("\u6211\u9700\u8981\u4e00\u6b21\u6027\u7684\u53eb\u9192\u670d\u52d9\u3002", "zh", 0.78, "zh-yue")
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


def test_end_call_trigger_when_guest_says_nothing_for_now():
    should_end, reason = should_end_call_deterministic("Nothing for now, thank you.")
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


def test_service_request_requires_confirmation_in_current_transcript():
    can_submit, reason = service_request_can_be_submitted(
        {"category": "room_service", "summary": "Prawn aglio olio", "confirmed_with_guest": True},
        "I'd like to order prawn aglio olio.",
    )

    assert can_submit is False
    assert "caller did not explicitly confirm" in reason


def test_service_request_allows_explicit_confirmation_transcript():
    can_submit, reason = service_request_can_be_submitted(
        {"category": "wake_up_call", "summary": "Wake-up call", "confirmed_with_guest": True},
        "Yes, that's correct.",
    )

    assert can_submit is True
    assert reason is None


def test_chinese_confirmation_transcript_is_recognized():
    assert transcript_confirms_service_request("\u6ca1\u6709\u95ee\u9898\u3002") is True
    assert transcript_confirms_service_request("\u53ef\u4ee5\u7684\uff0c\u73b0\u5728\u8fc7\u6765\u5427\u3002") is True


def test_wakeup_duplicate_ignores_summary_variation():
    first = {
        "category": "wake_up_call",
        "summary": "Wake-up call at 7 a.m.",
        "room_number": "1910",
        "alarm_time": "2026-05-11T07:00:00",
        "frequency": "Once",
    }
    repeated = {
        "category": "wake_up_call",
        "summary": "Wake-up call at 7 a.m. tomorrow",
        "room_number": "1910",
        "alarm_time": "2026-05-11T07:00:00",
        "frequency": "once",
    }

    assert service_request_fingerprint(first) == service_request_fingerprint(repeated)


def test_service_request_already_submitted_detects_duplicate():
    session = CallSession(call_id="dup-test")
    session.append_event(
        "service_request_submitted",
        {
            "sent": True,
            "payload": {
                "request": {
                    "category": "wake_up_call",
                    "summary": "Wake-up call at 7 a.m.",
                    "room_number": "1910",
                    "alarm_time": "2026-05-11T07:00:00",
                    "frequency": "Once",
                },
            },
        },
    )

    duplicate, matched = service_request_already_submitted(
        session,
        {
            "category": "wake_up_call",
            "summary": "Wake-up call at 7 a.m. tomorrow",
            "room_number": "1910",
            "alarm_time": "2026-05-11T07:00:00",
            "frequency": "once",
        },
    )

    assert duplicate is True
    assert matched["category"] == "wake_up_call"


def test_latest_pending_request_survives_duplicate_old_request():
    session = CallSession(call_id="pending-test")
    session.append_event(
        "service_request_submitted",
        {
            "sent": True,
            "payload": {
                "request": {
                    "category": "wake_up_call",
                    "summary": "Wake-up call at 7 a.m.",
                    "room_number": "1910",
                    "alarm_time": "2026-05-12T07:00:00",
                    "frequency": "Once",
                },
            },
        },
    )
    session.append_event(
        "service_request_confirmation_required",
        {
            "reason": "model marked request confirmed but caller did not explicitly confirm in this turn",
            "request": {
                "category": "room_service",
                "summary": "Order for prawn aglio olio and a Coke.",
                "room_number": "1910",
                "priority": "normal",
                "language": "en",
                "confirmed_with_guest": True,
            },
        },
    )

    pending = latest_unsubmitted_pending_service_request(session)

    assert pending["category"] == "room_service"
    assert pending["confirmed_with_guest"] is True


def test_submitted_pending_request_is_not_recovered_again():
    session = CallSession(call_id="pending-submitted-test")
    request = {
        "category": "room_service",
        "summary": "Order for prawn aglio olio and a Coke.",
        "room_number": "1910",
        "priority": "normal",
        "language": "en",
        "confirmed_with_guest": True,
    }
    session.append_event(
        "service_request_confirmation_required",
        {"reason": "needs confirmation", "request": request},
    )
    session.append_event(
        "service_request_submitted",
        {"sent": True, "payload": {"request": request}},
    )

    assert latest_unsubmitted_pending_service_request(session) is None


def test_service_request_confirmation_text_includes_summary_and_room():
    text = service_request_confirmation_text(
        {"summary": "spaghetti aglio e olio.", "room_number": "1002"},
        "en",
    )
    assert "Please confirm" in text
    assert "spaghetti aglio e olio" in text
    assert "1002" in text
    assert ". for room" not in text


def test_wakeup_call_request_skips_without_api_url(monkeypatch):
    monkeypatch.delenv("WAKEUP_CALL_API_URL", raising=False)
    session = CallSession(call_id="wake-test", room_number="1910")

    result = post_wakeup_call_request(
        session,
        {
            "category": "wake_up_call",
            "summary": "Wake-up call",
            "alarm_time": "2026-05-10T06:30:00",
            "confirmed_with_guest": True,
        },
    )

    assert result["sent"] is False
    assert "WAKEUP_CALL_API_URL" in result["reason"]
    assert result["payload"]["room_number"] == "1910"


def test_wakeup_frequency_is_normalized():
    assert normalize_wakeup_frequency("once") == "Once"
    assert normalize_wakeup_frequency("daily") == "Daily"
    assert normalize_wakeup_frequency("every week") == "Weekly"
    assert normalize_wakeup_frequency("unexpected") == "Once"


def test_wakeup_call_request_posts_to_api(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return b'{"status":"ok"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("WAKEUP_CALL_API_URL", "http://api.local/api/wakeup-call")
    monkeypatch.setenv("WAKEUP_CALL_API_TOKEN", "secret")
    monkeypatch.setenv("WAKEUP_CALL_API_TIMEOUT_SECONDS", "3")
    monkeypatch.setattr("voice_assistant_eagi.request.urlopen", fake_urlopen)
    session = CallSession(call_id="wake-test", caller_id="1000", caller_name="Guest", room_number="1910")

    result = post_wakeup_call_request(
        session,
        {
            "category": "wake_up_call",
            "summary": "Wake-up call",
            "alarm_time": "2026-05-10T06:30:00",
            "frequency": "once",
            "confirmed_with_guest": True,
        },
    )

    assert result["sent"] is True
    assert captured["url"] == "http://api.local/api/wakeup-call"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 3
    assert captured["body"]["room_number"] == "1910"
    assert captured["body"]["alarm_time"] == "2026-05-10T06:30:00"
    assert captured["body"]["frequency"] == "Once"


def test_rainbow_room_service_destination_uses_room_service_bubble(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination({"category": "room_service"})

    assert destination == "room_service"
    assert jid == "room-service@conference.openrainbow.com"


def test_rainbow_food_summary_uses_room_service_even_if_category_is_generic(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination(
        {"category": "general", "summary": "Order for Prawn Aglio Olio for room 1001"}
    )

    assert destination == "room_service"
    assert jid == "room-service@conference.openrainbow.com"


def test_rainbow_blank_room_service_categories_still_defaults_to_room_service(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_CATEGORIES", "")
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination({"category": "room_service", "summary": "Tea"})

    assert destination == "room_service"
    assert jid == "room-service@conference.openrainbow.com"


def test_rainbow_housekeeping_destination_uses_room_service_bubble(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_CATEGORIES", "room_service,housekeeping")
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination({"category": "housekeeping"})

    assert destination == "room_service"
    assert jid == "room-service@conference.openrainbow.com"


def test_rainbow_front_desk_category_uses_front_desk_bubble(monkeypatch):
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_CATEGORIES", "room_service,housekeeping")
    monkeypatch.setenv("RAINBOW_ROOM_SERVICE_BUBBLE_JID", "room-service@conference.openrainbow.com")
    monkeypatch.setenv("RAINBOW_FRONT_DESK_BUBBLE_JID", "front-desk@conference.openrainbow.com")

    destination, jid = rainbow_service_request_destination({"category": "front_desk", "summary": "Guest asks for manager"})

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
