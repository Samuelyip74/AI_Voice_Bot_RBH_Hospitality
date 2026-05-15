from __future__ import annotations

import math
import os
import select
import struct
import time
import wave
from contextlib import suppress
from pathlib import Path


DEFAULT_SAMPLE_RATE = 8000
OPENAI_PCM_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1
BIAS = 0x84
CLIP = 32635


class AudioCaptureError(RuntimeError):
    pass


def decode_telephony_audio(frame: bytes, codec: str = "ulaw") -> bytes:
    codec = codec.lower()
    if codec in {"ulaw", "pcmu", "mulaw"}:
        return b"".join(_ulaw_decode_byte(value) for value in frame)
    if codec in {"alaw", "pcma"}:
        return b"".join(_alaw_decode_byte(value) for value in frame)
    if codec in {"slin", "slin16", "pcm16"}:
        return frame
    raise ValueError(f"unsupported codec: {codec}")


def encode_telephony_audio(pcm16: bytes, codec: str = "ulaw") -> bytes:
    codec = codec.lower()
    if codec in {"ulaw", "pcmu", "mulaw"}:
        return bytes(_ulaw_encode_sample(sample) for sample in _iter_pcm16(pcm16))
    if codec in {"alaw", "pcma"}:
        return bytes(_alaw_encode_sample(sample) for sample in _iter_pcm16(pcm16))
    if codec in {"slin", "slin16", "pcm16"}:
        return pcm16
    raise ValueError(f"unsupported codec: {codec}")


def resample_pcm16(pcm16: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate:
        return pcm16
    samples = list(_iter_pcm16(pcm16))
    if not samples:
        return b""
    output_len = max(1, int(round(len(samples) * to_rate / from_rate)))
    if output_len == 1:
        return struct.pack("<h", samples[0])
    converted = bytearray()
    scale = (len(samples) - 1) / (output_len - 1)
    for i in range(output_len):
        pos = i * scale
        left = int(pos)
        right = min(left + 1, len(samples) - 1)
        fraction = pos - left
        value = int(samples[left] * (1.0 - fraction) + samples[right] * fraction)
        converted.extend(struct.pack("<h", max(-32768, min(32767, value))))
    return bytes(converted)


class Pcm16Resampler:
    """Stateful mono PCM16 resampler for live chunked streams."""

    def __init__(self, from_rate: int, to_rate: int) -> None:
        self.from_rate = from_rate
        self.to_rate = to_rate
        self._state = None
        self._audioop = None
        with suppress(Exception):
            import audioop

            self._audioop = audioop

    def process(self, pcm16: bytes) -> bytes:
        if self.from_rate == self.to_rate:
            return pcm16
        if not pcm16:
            return b""
        if self._audioop is None:
            return resample_pcm16(pcm16, self.from_rate, self.to_rate)
        converted, self._state = self._audioop.ratecv(
            pcm16,
            SAMPLE_WIDTH,
            CHANNELS,
            self.from_rate,
            self.to_rate,
            self._state,
        )
        return converted


def rms_dbfs(pcm16: bytes) -> float:
    if not pcm16:
        return -120.0
    samples = list(_iter_pcm16(pcm16))
    if not samples:
        return -120.0
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    if rms <= 0:
        return -120.0
    return 20.0 * math.log10(rms / 32768.0)


def pcm_duration_seconds(pcm16: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE) -> float:
    if sample_rate <= 0:
        return 0.0
    return len(pcm16) / (sample_rate * SAMPLE_WIDTH)


def speech_ratio(pcm16: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE, frame_ms: int = 20, threshold_dbfs: float = -38.0) -> float:
    frame_bytes = max(SAMPLE_WIDTH, int(sample_rate * frame_ms / 1000) * SAMPLE_WIDTH)
    if not pcm16:
        return 0.0
    frames = 0
    speech_frames = 0
    for offset in range(0, len(pcm16), frame_bytes):
        frame = pcm16[offset : offset + frame_bytes]
        if len(frame) < frame_bytes:
            continue
        frames += 1
        if not is_silence(frame, threshold_dbfs):
            speech_frames += 1
    return speech_frames / frames if frames else 0.0


def is_silence(pcm16: bytes, threshold_dbfs: float = -42.0) -> bool:
    return rms_dbfs(pcm16) < threshold_dbfs


def drain_audio_fd(fd: int = 3, frame_bytes: int = 320, drain_ms: int = 250) -> int:
    """Discard already-buffered EAGI audio, usually after playback.

    EAGI fd 3 can keep receiving inbound RTP while AGI is streaming a prompt.
    Draining briefly prevents the next turn from transcribing prompt echo, line
    comfort noise, or speech that happened during playback as fresh user input.
    """
    if drain_ms <= 0:
        return 0
    try:
        os.set_blocking(fd, False)
    except (AttributeError, OSError):
        pass

    drained = 0
    deadline = time.monotonic() + drain_ms / 1000.0
    while time.monotonic() < deadline:
        wait_seconds = max(0.0, min(0.02, deadline - time.monotonic()))
        readable, _writable, _error = select.select([fd], [], [], wait_seconds)
        if not readable:
            continue
        try:
            frame = os.read(fd, frame_bytes)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not frame:
            break
        drained += len(frame)
    return drained


def capture_utterance_from_fd3(
    fd: int = 3,
    codec: str = "ulaw",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    frame_ms: int = 20,
    silence_timeout_ms: int = 900,
    max_seconds: int = 15,
    silence_threshold_dbfs: float = -42.0,
    initial_audio_timeout_seconds: float = 8.0,
    no_speech_timeout_seconds: float = 5.0,
) -> bytes:
    """Read caller audio from EAGI fd 3 until trailing silence or max length.

    Asterisk EAGI exposes inbound channel audio as raw frames on fd 3 while stdin/stdout remain
    reserved for AGI protocol. This function returns mono PCM16 at the source sample rate.
    """
    encoded_bytes_per_frame = int(sample_rate * frame_ms / 1000)
    if codec in {"slin", "slin16", "pcm16"}:
        encoded_bytes_per_frame *= SAMPLE_WIDTH

    chunks: list[bytes] = []
    speech_started = False
    silence_ms = 0
    started_at = time.monotonic()
    deadline = time.monotonic() + max_seconds
    first_frame_deadline = time.monotonic() + initial_audio_timeout_seconds
    try:
        os.set_blocking(fd, False)
    except (AttributeError, OSError):
        pass

    while time.monotonic() < deadline:
        wait_deadline = first_frame_deadline if not chunks else deadline
        wait_seconds = max(0.0, min(0.5, wait_deadline - time.monotonic()))
        if wait_seconds == 0.0:
            break
        readable, _writable, _error = select.select([fd], [], [], wait_seconds)
        if not readable:
            continue
        try:
            frame = os.read(fd, encoded_bytes_per_frame)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise AudioCaptureError(f"failed to read EAGI audio fd {fd}: {exc}") from exc
        if not frame:
            break

        pcm = decode_telephony_audio(frame, codec)
        chunks.append(pcm)

        if is_silence(pcm, silence_threshold_dbfs):
            if not speech_started and time.monotonic() - started_at >= no_speech_timeout_seconds:
                break
            if speech_started:
                silence_ms += frame_ms
                if silence_ms >= silence_timeout_ms:
                    break
        else:
            speech_started = True
            silence_ms = 0

    return b"".join(chunks)


def write_wav(path: str | Path, pcm16: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16)
    return target


def read_wav_pcm16(path: str | Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1:
            raise ValueError("expected mono WAV")
        if wav.getsampwidth() != SAMPLE_WIDTH:
            raise ValueError("expected PCM16 WAV")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def openai_input_pcm(pcm16: bytes, source_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    return resample_pcm16(pcm16, source_rate, OPENAI_PCM_RATE)


def asterisk_playback_wav(path: str | Path, openai_pcm24k: bytes, output_rate: int = DEFAULT_SAMPLE_RATE) -> Path:
    pcm = resample_pcm16(openai_pcm24k, OPENAI_PCM_RATE, output_rate)
    return write_wav(path, pcm, output_rate)


def _iter_pcm16(pcm16: bytes):
    usable = len(pcm16) - (len(pcm16) % 2)
    for (sample,) in struct.iter_unpack("<h", pcm16[:usable]):
        yield sample


def _ulaw_decode_byte(value: int) -> bytes:
    value = ~value & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + BIAS) << exponent
    sample -= BIAS
    if sign:
        sample = -sample
    return struct.pack("<h", max(-32768, min(32767, sample)))


def _ulaw_encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(sample), CLIP) + BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _alaw_decode_byte(value: int) -> bytes:
    value ^= 0x55
    sign = value & 0x80
    exponent = (value & 0x70) >> 4
    mantissa = value & 0x0F
    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)
    if not sign:
        sample = -sample
    return struct.pack("<h", max(-32768, min(32767, sample)))


def _alaw_encode_sample(sample: int) -> int:
    sign = 0x80 if sample >= 0 else 0
    sample = min(abs(sample), 32635)
    if sample < 256:
        exponent = 0
        mantissa = sample >> 4
    else:
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not (sample & mask):
            mask >>= 1
            exponent -= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
    return (sign | (exponent << 4) | mantissa) ^ 0x55
