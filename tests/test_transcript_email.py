import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession
from voice_assistant_eagi import send_call_transcript_email, transcript_text


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
