import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from voice_assistant_eagi import agi_response_is_dead_channel, agi_result_code


def test_agi_result_code_parses_success():
    assert agi_result_code("200 result=6") == 6


def test_agi_result_code_parses_failure_with_text():
    assert agi_result_code("200 result=1 (FAILURE)") == 1


def test_dead_channel_response_detected():
    assert agi_response_is_dead_channel("511 Command Not Permitted on a dead channel or intercept routine")
