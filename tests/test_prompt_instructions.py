import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from call_session import CallSession
from openai_realtime_client import ASSISTANT_INSTRUCTIONS, build_prior_call_context


def test_prompt_requires_context_intake():
    assert "collect the minimum missing context" in ASSISTANT_INSTRUCTIONS
    assert "Ask one focused question at a time" in ASSISTANT_INSTRUCTIONS
    assert "Do not ask for details already known" in ASSISTANT_INSTRUCTIONS
    assert "use that room number for hotel service requests" in ASSISTANT_INSTRUCTIONS
    assert "Do not reintroduce yourself as Nova again" in ASSISTANT_INSTRUCTIONS
    assert "Do not call transfer_to_extension merely because the guest asks for food" in ASSISTANT_INSTRUCTIONS


def test_prompt_has_required_hospitality_workflows():
    assert "Wake-up call: room number, date, time" in ASSISTANT_INSTRUCTIONS
    assert "alarm_time" in ASSISTANT_INSTRUCTIONS
    assert "local hotel ISO format" in ASSISTANT_INSTRUCTIONS
    assert "Housekeeping: room number" in ASSISTANT_INSTRUCTIONS
    assert "Transportation: pickup/drop-off location" in ASSISTANT_INSTRUCTIONS
    assert "Dining recommendation or reservation" in ASSISTANT_INSTRUCTIONS
    assert "submit_hotel_request with category housekeeping" in ASSISTANT_INSTRUCTIONS
    assert "submit_hotel_request with category room_service" in ASSISTANT_INSTRUCTIONS
    assert "Only after confirmation" in ASSISTANT_INSTRUCTIONS
    assert "Never call submit_hotel_request with confirmed_with_guest false" in ASSISTANT_INSTRUCTIONS


def test_prompt_preserves_transfer_routes():
    assert "transfer_to_extension tool with extension 1920" in ASSISTANT_INSTRUCTIONS
    assert "transfer_to_extension tool with extension 1921" in ASSISTANT_INSTRUCTIONS
    assert "Transfer to 1921 only if the guest explicitly asks" in ASSISTANT_INSTRUCTIONS
    assert "Do not use this route for routine service-intake requests" in ASSISTANT_INSTRUCTIONS
    assert "Direct room transfer" in ASSISTANT_INSTRUCTIONS
    assert "transfer_type set to room" in ASSISTANT_INSTRUCTIONS


def test_prompt_can_end_call_when_guest_is_done():
    assert "no more requests" in ASSISTANT_INSTRUCTIONS
    assert "end_call tool" in ASSISTANT_INSTRUCTIONS
    assert "Do not ask another follow-up question" in ASSISTANT_INSTRUCTIONS


def test_prior_call_context_keeps_known_room_after_silence_events():
    session = CallSession(call_id="ctx", caller_id="1000", preferred_language="zh")
    session.history = [
        {"type": "user", "role": "user", "text": "我的房间很肮脏，可以帮我清理吗？"},
        {"type": "assistant", "role": "assistant", "text": "请问您的房间号是多少？"},
        {"type": "user", "role": "user", "text": "我的房间号码是1001。"},
        {"type": "audio_ignored", "reason": "audio rms below -46 dBFS"},
        {"type": "silence_prompt_played", "text": "请问您还在线吗？"},
        {"type": "user", "role": "user", "text": "现在就叫工作人员来清理我的房间，谢谢。"},
    ]

    context = build_prior_call_context(session)

    assert "1001" in context
    assert "Known details" in context
    assert "audio_ignored" not in context
    assert "silence_prompt_played" not in context


def test_prior_call_context_includes_room_from_caller_identity():
    session = CallSession(
        call_id="ctx",
        caller_id="99902200110783813606957949",
        caller_name="Samuel Yip - 1910 - EN",
        room_number="1910",
    )
    session.history = [{"type": "call_started", "caller_name": session.caller_name, "room_number": session.room_number}]

    context = build_prior_call_context(session)

    assert "Caller room number from caller identity: 1910" in context
    assert '"room_number": "1910"' in context


def test_turn_instructions_include_known_room(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from openai_realtime_client import OpenAIRealtimeClient

    client = OpenAIRealtimeClient()
    session = CallSession(call_id="ctx", room_number="1910")

    instructions = client._turn_instructions(session)

    assert "Known caller room number is 1910" in instructions
    assert "do not ask for the room number again" in instructions


def test_turn_instructions_include_hotel_datetime(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from openai_realtime_client import OpenAIRealtimeClient

    client = OpenAIRealtimeClient()
    instructions = client._turn_instructions(CallSession(call_id="ctx"))

    assert "Hotel local datetime is" in instructions
    assert "Asia/Singapore" in instructions
