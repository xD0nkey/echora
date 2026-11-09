"""Low-latency, glitch-free audio path for AirBlade.

Windows loopback (float32, device rate)
  -> soxr high-quality resampling to 44.1 kHz
  -> int16 big-endian (what AirPlay/ALAC expects)
  -> one LiveSource ring buffer per speaker
  -> pyatv sender (always gets *full* packets, never zero-padded mid-music)

This replaces pyatv's file/stream decoding (miniaudio + several small
buffers), which returned partial packets that pyatv padded with silence and
which caused audible stutter.
"""
import asyncio
import ctypes
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio
import soxr
from pyatv.protocols.raop import audio_source as _as
import pyatv.protocols.raop as _raop
from pyatv.support.metadata import EMPTY_METADATA

OUT_RATE = 44100
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2


def boost_timing() -> None:
    """1 ms Windows timer resolution + higher priority = steadier packet pacing."""
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
    except Exception:
        pass


class LiveSource(_as.AudioSource):
    """Ring buffer of 44.1 kHz s16 BE stereo audio consumed by pyatv."""

    def __init__(self, capture: "Capture", target_ms: int = 60, max_ms: int = 250):
        self.capture = capture
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.target = int(OUT_RATE * target_ms / 1000) * FRAME_BYTES
        self.max = int(OUT_RATE * max_ms / 1000) * FRAME_BYTES
        self.closed = False
        self.underruns = 0
        self.dropped_ms = 0.0
        self._primed = False
        self._started = False

    # --- producer side (capture thread)
    def push(self, data: bytes) -> None:
        if not self._started:
            return  # still connecting; don't queue up stale audio
        with self.lock:
            self.buf += data
            # Capture clock running faster than the network clock: trim the
            # excess so delay never creeps up (rare, a few ms at a time).
            if len(self.buf) > self.max:
                excess = len(self.buf) - self.target
                excess -= excess % FRAME_BYTES
                del self.buf[:excess]
                self.dropped_ms += excess / FRAME_BYTES / OUT_RATE * 1000

    # --- consumer side (pyatv, asyncio loop)
    async def readframes(self, nframes: int) -> bytes:
        need = nframes * FRAME_BYTES
        if self.closed:
            return self.NO_FRAMES
        if not self._started:
            # Discard audio captured while we were connecting (stale).
            self._started = True
            with self.lock:
                self.buf.clear()
        # Wait briefly for a full packet while audio is flowing.
        deadline = time.monotonic() + 0.05
        while len(self.buf) < (need if self._primed else need + self.target):
            if not self.capture.active() or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.002)
        with self.lock:
            if len(self.buf) >= need:
                out = bytes(self.buf[:need])
                del self.buf[:need]
                self._primed = True
                return out
            # Not enough audio: Windows is silent (or capture hiccup).
            if self.capture.active():
                self.underruns += 1
            self._primed = False
            out = bytes(self.buf) + bytes(need - len(self.buf))
            self.buf.clear()
            return out

    async def close(self) -> None:
        self.closed = True

    async def get_metadata(self):
        return EMPTY_METADATA

    @property
    def sample_rate(self) -> int:
        return OUT_RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    @property
    def sample_size(self) -> int:
        return 2

    @property
    def duration(self) -> int:
        return 0


_orig_open_source = _raop.open_source


async def _open_source(source, *args, **kwargs):
    if isinstance(source, LiveSource):
        return source
    return await _orig_open_source(source, *args, **kwargs)


_raop.open_source = _open_source


class Capture(threading.Thread):
    """WASAPI loopback capture of the default speakers, resampled once for all."""

    def __init__(self):
        super().__init__(daemon=True)
        self.pa = pyaudio.PyAudio()
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        spk = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not spk["isLoopbackDevice"]:
            for lb in self.pa.get_loopback_device_info_generator():
                if spk["name"] in lb["name"]:
                    spk = lb
                    break
        self.device = spk
        self.name = spk["name"].replace(" [Loopback]", "")
        self.rate = int(spk["defaultSampleRate"])
        self.in_channels = int(spk["maxInputChannels"]) or 2
        self.sources: list[LiveSource] = []
        self.lock = threading.Lock()
        self.running = True
        self.level = 0.0
        self.last_data = 0.0
        self.resampler = (soxr.ResampleStream(self.rate, OUT_RATE, CHANNELS, dtype="float32", quality="VHQ")
                          if self.rate != OUT_RATE else None)

    def active(self) -> bool:
        """True while Windows is actually delivering audio."""
        return time.monotonic() - self.last_data < 0.15

    def new_source(self) -> LiveSource:
        s = LiveSource(self)
        with self.lock:
            self.sources.append(s)
        return s

    def remove_source(self, s: LiveSource) -> None:
        with self.lock:
            if s in self.sources:
                self.sources.remove(s)
        s.closed = True

    def run(self):
        try:
            ctypes.windll.avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(ctypes.c_ulong(0)))
        except Exception:
            pass
        frames = int(self.rate / 100)  # 10 ms blocks
        stream = self.pa.open(format=pyaudio.paFloat32, channels=self.in_channels,
                              rate=self.rate, input=True, frames_per_buffer=frames,
                              input_device_index=self.device["index"])
        while self.running:
            try:
                raw = stream.read(frames, exception_on_overflow=False)
            except Exception:
                time.sleep(0.01)
                continue
            self.last_data = time.monotonic()
            x = np.frombuffer(raw, dtype=np.float32).reshape(-1, self.in_channels)
            if self.in_channels == 1:
                x = np.repeat(x, 2, axis=1)
            elif self.in_channels > 2:
                x = x[:, :2]
            if self.resampler:
                x = self.resampler.resample_chunk(np.ascontiguousarray(x))
            if not len(x):
                continue
            peak = float(np.abs(x[::8]).max()) if len(x) else 0.0
            self.level = max(peak, self.level * 0.85)
            pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(">i2").tobytes()
            with self.lock:
                for s in self.sources:
                    s.push(pcm)
        stream.close()
        self.pa.terminate()
