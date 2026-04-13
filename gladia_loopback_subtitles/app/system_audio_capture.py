from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover - optional dependency at runtime
    pyaudio = None

try:
    import soundcard as sc
except ImportError:  # pragma: no cover - optional dependency at runtime
    sc = None


@dataclass(slots=True)
class AudioChunkConfig:
    capture_sample_rate: int
    capture_channels: int
    target_sample_rate: int
    chunk_duration_ms: int


class SystemAudioCapture:
    def __init__(self, config: AudioChunkConfig) -> None:
        self.config = config
        self.backend = ""
        self.device_name = ""
        self.last_level = 0.0
        self._stop_requested = threading.Event()

        self._frames_per_chunk = max(
            1, int(self.config.capture_sample_rate * self.config.chunk_duration_ms / 1000)
        )

        self._pyaudio_instance: Any | None = None
        self._pyaudio_stream: Any | None = None
        self._pyaudio_channels = max(1, self.config.capture_channels)
        self._pyaudio_rate = self.config.capture_sample_rate

        self._speaker: Any | None = None
        self._microphone: Any | None = None
        self._recorder_cm: Any | None = None
        self._recorder: Any | None = None

    def open(self) -> None:
        self._stop_requested.clear()
        pyaudio_error: Exception | None = None
        if pyaudio is not None:
            try:
                self._open_with_pyaudio()
                return
            except Exception as exc:  # pragma: no cover - depends on host audio stack
                pyaudio_error = exc
                self.close()

        if sc is not None:
            try:
                self._open_with_soundcard()
                return
            except Exception as exc:  # pragma: no cover - depends on host audio stack
                self.close()
                if pyaudio_error is not None:
                    raise RuntimeError(
                        "Failed to open loopback capture with both PyAudioWPatch and SoundCard."
                    ) from exc
                raise

        if pyaudio_error is not None:
            raise RuntimeError("Failed to open loopback capture with PyAudioWPatch.") from pyaudio_error

        raise RuntimeError("No supported Windows loopback backend is available.")

    def close(self) -> None:
        self._stop_requested.set()
        if self._pyaudio_stream is not None:
            self._pyaudio_stream.stop_stream()
            self._pyaudio_stream.close()
            self._pyaudio_stream = None

        if self._pyaudio_instance is not None:
            self._pyaudio_instance.terminate()
            self._pyaudio_instance = None

        if self._recorder_cm is not None:
            self._recorder_cm.__exit__(None, None, None)
            self._recorder_cm = None
            self._recorder = None

    def request_stop(self) -> None:
        self._stop_requested.set()

    def read_chunk(self) -> bytes:
        if self.backend == "pyaudiowpatch":
            if self._pyaudio_stream is None:
                raise RuntimeError("PyAudio loopback stream is not open.")

            while not self._stop_requested.is_set():
                read_available = int(self._pyaudio_stream.get_read_available())
                if read_available >= self._frames_per_chunk:
                    break
                time.sleep(0.01)

            if self._stop_requested.is_set():
                self.last_level = 0.0
                return b""

            raw = self._pyaudio_stream.read(self._frames_per_chunk, exception_on_overflow=False)
            return self._prepare_interleaved_pcm16(
                raw_bytes=raw,
                source_rate=self._pyaudio_rate,
                source_channels=self._pyaudio_channels,
            )

        if self.backend == "soundcard":
            if self._recorder is None:
                raise RuntimeError("SoundCard recorder is not open.")

            if self._stop_requested.is_set():
                self.last_level = 0.0
                return b""

            frames = self._recorder.record(numframes=self._frames_per_chunk)
            return self._prepare_float_frames(frames, self.config.capture_sample_rate)

        raise RuntimeError("Loopback capture backend is not open.")

    def _open_with_pyaudio(self) -> None:
        instance = pyaudio.PyAudio()
        wasapi_info = instance.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = instance.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        device = default_output

        if not device.get("isLoopbackDevice"):
            for loopback in instance.get_loopback_device_info_generator():
                if device["name"] in loopback["name"]:
                    device = loopback
                    break

        channels = max(1, min(self.config.capture_channels, int(device["maxInputChannels"] or 1)))
        rate = int(device["defaultSampleRate"])
        frames_per_chunk = max(1, int(rate * self.config.chunk_duration_ms / 1000))

        stream = instance.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            frames_per_buffer=frames_per_chunk,
            input_device_index=device["index"],
        )

        self._pyaudio_instance = instance
        self._pyaudio_stream = stream
        self._pyaudio_channels = channels
        self._pyaudio_rate = rate
        self._frames_per_chunk = frames_per_chunk
        self.backend = "pyaudiowpatch"
        self.device_name = str(device["name"])

    def _open_with_soundcard(self) -> None:
        self._speaker = sc.default_speaker()
        if self._speaker is None:
            raise RuntimeError("No default speaker found. Please make sure Windows has an active output device.")

        self._microphone = sc.get_microphone(str(self._speaker.name), include_loopback=True)
        if self._microphone is None:
            raise RuntimeError("Failed to create a loopback capture device for the default speaker.")

        last_error: Exception | None = None
        for channels in (self.config.capture_channels, 1):
            try:
                recorder_cm = self._microphone.recorder(
                    samplerate=self.config.capture_sample_rate,
                    channels=channels,
                    blocksize=self._frames_per_chunk,
                )
                recorder = recorder_cm.__enter__()
            except Exception as exc:  # pragma: no cover - depends on host audio stack
                last_error = exc
                continue

            self._recorder_cm = recorder_cm
            self._recorder = recorder
            self.backend = "soundcard"
            self.device_name = str(self._speaker.name)
            return

        raise RuntimeError(
            "Unable to open the Windows loopback recorder. "
            "Try changing CAPTURE_SAMPLE_RATE / CAPTURE_CHANNELS in .env."
        ) from last_error

    def _prepare_interleaved_pcm16(self, raw_bytes: bytes, source_rate: int, source_channels: int) -> bytes:
        audio = np.frombuffer(raw_bytes, dtype=np.int16)
        if source_channels > 1 and audio.size:
            audio = audio.reshape(-1, source_channels).astype(np.float32)
            audio = audio.mean(axis=1)
        else:
            audio = audio.astype(np.float32)

        audio = np.clip(audio / 32768.0, -1.0, 1.0)
        audio = self._resample(audio, source_rate, self.config.target_sample_rate)
        self.last_level = self._calculate_level(audio)
        pcm = np.asarray(audio * 32767.0, dtype=np.int16)
        return pcm.tobytes()

    def _prepare_float_frames(self, frames: np.ndarray, source_rate: int) -> bytes:
        audio = np.asarray(frames, dtype=np.float32)

        if audio.ndim == 2 and audio.shape[1] > 1:
            audio = audio.mean(axis=1)
        elif audio.ndim == 2:
            audio = audio[:, 0]

        audio = np.clip(audio, -1.0, 1.0)
        audio = self._resample(audio, source_rate, self.config.target_sample_rate)
        self.last_level = self._calculate_level(audio)
        pcm = np.asarray(audio * 32767.0, dtype=np.int16)
        return pcm.tobytes()

    @staticmethod
    def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
        if source_rate == target_rate or audio.size == 0:
            return audio.astype(np.float32, copy=False)

        duration_seconds = audio.shape[0] / source_rate
        target_samples = max(1, int(round(duration_seconds * target_rate)))
        source_positions = np.linspace(0.0, 1.0, num=audio.shape[0], endpoint=False)
        target_positions = np.linspace(0.0, 1.0, num=target_samples, endpoint=False)
        return np.interp(target_positions, source_positions, audio).astype(np.float32)

    @staticmethod
    def _calculate_level(audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0

        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float32), dtype=np.float32)))
        return max(0.0, min(1.0, rms * 4.0))
