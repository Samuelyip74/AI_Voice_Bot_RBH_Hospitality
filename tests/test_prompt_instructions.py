import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from openai_realtime_client import ASSISTANT_INSTRUCTIONS


def test_prompt_requires_context_intake():
    assert "collect the minimum missing context" in ASSISTANT_INSTRUCTIONS
    assert "Ask one focused question at a time" in ASSISTANT_INSTRUCTIONS
    assert "Do not ask for details already known" in ASSISTANT_INSTRUCTIONS
    assert "Do not reintroduce yourself as Nova again" in ASSISTANT_INSTRUCTIONS
    assert "Do not call transfer_to_extension merely because the guest asks for food" in ASSISTANT_INSTRUCTIONS


def test_prompt_has_required_hospitality_workflows():
    assert "Wake-up call: room number, date, time" in ASSISTANT_INSTRUCTIONS
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
