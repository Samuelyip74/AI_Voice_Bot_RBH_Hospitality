import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agi"))

from audio_utils import (
    OPENAI_PCM_RATE,
    asterisk_playback_wav,
    decode_telephony_audio,
    encode_telephony_audio,
    openai_input_pcm,
    pcm_duration_seconds,
    read_wav_pcm16,
    rms_dbfs,
    speech_ratio,
)


def sine_pcm(rate=8000, seconds=0.1, hz=440):
    frames = []
    for i in range(int(rate * seconds)):
        sample = int(12000 * math.sin(2 * math.pi * hz * i / rate))
        frames.append(struct.pack("<h", sample))
    return b"".join(frames)


def test_ulaw_roundtrip_has_audio():
    pcm = sine_pcm()
    encoded = encode_telephony_audio(pcm, "ulaw")
    decoded = decode_telephony_audio(encoded, "ulaw")
    assert len(decoded) == len(pcm)
    assert decoded != b"\x00" * len(decoded)


def test_openai_input_resamples_to_24k():
    pcm = sine_pcm(rate=8000, seconds=1)
    converted = openai_input_pcm(pcm, 8000)
    assert len(converted) == OPENAI_PCM_RATE * 2


def test_asterisk_playback_wav(tmp_path):
    pcm24k = sine_pcm(rate=24000, seconds=0.25)
    wav = asterisk_playback_wav(tmp_path / "response.wav", pcm24k, output_rate=8000)
    data, rate = read_wav_pcm16(wav)
    assert rate == 8000
    assert len(data) == 8000 * 2 // 4


def test_audio_quality_metrics_distinguish_speech_from_silence():
    speech = sine_pcm(rate=8000, seconds=1)
    silence = b"\x00\x00" * 8000

    assert pcm_duration_seconds(speech, 8000) == 1.0
    assert rms_dbfs(speech) > -20
    assert speech_ratio(speech, 8000) > 0.9
    assert rms_dbfs(silence) == -120.0
    assert speech_ratio(silence, 8000) == 0.0
