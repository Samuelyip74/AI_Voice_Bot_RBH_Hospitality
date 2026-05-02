import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession
from voice_assistant_eagi import send_call_transcript_email, send_service_request_email, smtp_config, transcript_text


def test_transcript_text_includes_user_and_assistant():
    session = CallSession(call_id="email-test", caller_id="1000")
    session.log_dir = Path("logs/test-calls")
    session.history = [
        {"type": "user", "text": "Please clean my room."},
        {"type": "assistant", "text": "Of course. May I have your room number?"},
    ]

    text = transcript_text(session)

    assert "Call ID: email-test" in text
    assert "Guest: Please clean my room." in text
    assert "Assistant: Of course. May I have your room number?" in text


def test_transcript_email_skips_without_smtp(monkeypatch):
    monkeypatch.setenv("EMAIL_TRANSCRIPT_ENABLED", "true")
    monkeypatch.setenv("TRANSCRIPT_EMAIL_TO", "kahyean.yip+pdopenai@gmail.com")
    monkeypatch.setenv("SMTP_HOST", "")
    session = CallSession(call_id="email-test", caller_id="1000")
    session.log_dir = Path("logs/test-calls")

    result = send_call_transcript_email(session)

    assert result["sent"] is False
    assert "SMTP_HOST" in result["reason"]


def test_service_request_email_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("SERVICE_REQUEST_EMAIL_ENABLED", "false")
    session = CallSession(call_id="request-email-test", caller_id="1000")
    session.log_dir = Path("logs/test-calls")

    result = send_service_request_email(session, {"category": "room_service", "summary": "Tea"}, {"sent": True})

    assert result["sent"] is False
    assert "SERVICE_REQUEST_EMAIL_ENABLED" in result["reason"]


def test_service_request_email_uses_transcript_recipient_and_requires_smtp(monkeypatch):
    monkeypatch.setenv("SERVICE_REQUEST_EMAIL_ENABLED", "true")
    monkeypatch.setenv("SERVICE_REQUEST_EMAIL_TO", "")
    monkeypatch.setenv("TRANSCRIPT_EMAIL_TO", "hotel@example.com")
    monkeypatch.setenv("SMTP_HOST", "")
    session = CallSession(call_id="request-email-test", caller_id="1000")
    session.log_dir = Path("logs/test-calls")

    result = send_service_request_email(
        session,
        {"category": "room_service", "summary": "Tea", "confirmed_with_guest": True},
        {"sent": True, "status": 200},
    )

    assert result["sent"] is False
    assert "SMTP_HOST" in result["reason"]


def test_smtp_config_formats_sender_display_name(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_EMAIL_FROM", "voicebot@example.com")
    monkeypatch.setenv("EMAIL_FROM_NAME", "Hotel Voicebot")

    config = smtp_config()

    assert config["sender"] == "voicebot@example.com"
    assert config["sender_header"] == "Hotel Voicebot <voicebot@example.com>"
