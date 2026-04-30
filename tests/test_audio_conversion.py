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
    read_wav_pcm16,
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
