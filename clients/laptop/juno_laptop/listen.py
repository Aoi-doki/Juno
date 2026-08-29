"""Wake word, endpointing and transcription — all local.

The microphone is open continuously, but nothing leaves the machine and no
model larger than openWakeWord's few-hundred-kilobyte classifier runs until the
wake word fires. Whisper is loaded once and only invoked on a captured
utterance, so idle cost is a fraction of one core.

The same audio stream serves three jobs at once: wake-word detection when idle,
utterance capture after a trigger, and barge-in detection while Juno is
talking. One stream rather than three avoids fighting over the input device,
which is what usually breaks these setups on Linux.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Callable

import numpy as np

from juno_laptop.config import FRAME_SAMPLES, SAMPLE_RATE, VAD_FRAME_MS, ClientConfig

log = logging.getLogger(__name__)

# Keep this much audio from before the wake word fired. People habitually run
# the trigger into the request — "hey juno what's my day" — and without a
# pre-roll the first word or two is lost.
PREROLL_MS = 500


class Endpointer:
    """Decides when the user has stopped talking.

    Pure logic, no audio dependencies, so the timing rules are testable without
    a microphone: feed it voiced/unvoiced decisions and it tells you when the
    utterance is done.
    """

    def __init__(self, silence_ms: int, max_seconds: float, frame_ms: int = VAD_FRAME_MS) -> None:
        self.frames_of_silence_needed = max(1, silence_ms // frame_ms)
        self.max_frames = int(max_seconds * 1000 / frame_ms)
        self.frame_ms = frame_ms
        self.reset()

    def reset(self) -> None:
        self._silent_run = 0
        self._frames = 0
        self._heard_anything = False

    @property
    def heard_speech(self) -> bool:
        return self._heard_anything

    def feed(self, voiced: bool) -> bool:
        """Returns True when the utterance is complete."""
        self._frames += 1
        if voiced:
            self._heard_anything = True
            self._silent_run = 0
        else:
            self._silent_run += 1

        if self._frames >= self.max_frames:
            return True
        # Leading silence must not end the utterance before it starts — someone
        # who takes a breath after the wake word would be cut off at once.
        if not self._heard_anything:
            return False
        return self._silent_run >= self.frames_of_silence_needed


class Listener:
    """Owns the input device, the wake-word model and Whisper."""

    def __init__(
        self,
        config: ClientConfig,
        on_utterance: Callable[[str], object],
        speaking: Callable[[], bool],
        on_barge_in: Callable[[], object],
    ) -> None:
        self.config = config
        self._on_utterance = on_utterance
        self._speaking = speaking
        self._on_barge_in = on_barge_in

        self._wake = None
        self._whisper = None
        self._vad = None
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self._preroll: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(1, PREROLL_MS // (FRAME_SAMPLES * 1000 // SAMPLE_RATE))
        )

    # --- model loading -------------------------------------------------------

    def _load_models(self) -> None:
        import webrtcvad
        from faster_whisper import WhisperModel
        from openwakeword.model import Model as WakeModel

        log.info("loading wake word %r", self.config.wake_word)
        self._wake = WakeModel(wakeword_models=[self.config.wake_word], inference_framework="onnx")

        log.info("loading Whisper %s", self.config.whisper_model)
        self._whisper = WhisperModel(
            self.config.whisper_model, device="cpu", compute_type=self.config.whisper_compute
        )

        # Aggressiveness 2 of 0-3: rejects keyboard noise and fans without
        # clipping quiet speech.
        self._vad = webrtcvad.Vad(2)

    # --- audio ---------------------------------------------------------------

    async def run(self) -> None:
        import sounddevice as sd

        await asyncio.to_thread(self._load_models)
        loop = asyncio.get_running_loop()

        def callback(indata, _frames, _time, status):  # noqa: ANN001
            if status:
                log.debug("input status: %s", status)
            # Copy: sounddevice reuses the buffer after the callback returns.
            frame = indata[:, 0].copy()
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, frame)
            except RuntimeError:
                pass

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            callback=callback,
        )
        with stream:
            log.info("listening for %r", self.config.wake_word)
            await self._loop()

    async def _loop(self) -> None:
        barge_in_run = 0
        while True:
            try:
                frame = await self._queue.get()
            except asyncio.CancelledError:
                return

            # While she is talking, the microphone is only used to notice that
            # she should stop. Running wake-word detection here as well would
            # trigger on her own voice through the speakers.
            if self._speaking():
                if not self.config.enable_barge_in:
                    continue
                if self._any_voiced(frame):
                    barge_in_run += 1
                    if barge_in_run >= self.config.barge_in_frames:
                        barge_in_run = 0
                        self._on_barge_in()
                else:
                    barge_in_run = 0
                continue

            barge_in_run = 0
            self._preroll.append(frame)

            if self._detect_wake(frame):
                log.info("wake word detected")
                await self._capture_and_transcribe()

    def _detect_wake(self, frame: np.ndarray) -> bool:
        # openWakeWord expects 16-bit PCM.
        pcm = (frame * 32767).astype(np.int16)
        scores = self._wake.predict(pcm)
        hit = any(score >= self.config.wake_threshold for score in scores.values())
        if hit:
            # Without this the same utterance retriggers on the next few frames,
            # since the model's buffer still contains the wake word.
            self._wake.reset()
        return hit

    def _any_voiced(self, frame: np.ndarray) -> bool:
        return any(self._voiced(sub) for sub in self._split_vad_frames(frame))

    def _split_vad_frames(self, frame: np.ndarray) -> list[np.ndarray]:
        size = SAMPLE_RATE * VAD_FRAME_MS // 1000
        return [frame[i : i + size] for i in range(0, len(frame) - size + 1, size)]

    def _voiced(self, sub: np.ndarray) -> bool:
        pcm = (sub * 32767).astype(np.int16).tobytes()
        try:
            return self._vad.is_speech(pcm, SAMPLE_RATE)
        except Exception:  # noqa: BLE001 - malformed frame length; treat as silence
            return False

    async def _capture_and_transcribe(self) -> None:
        endpointer = Endpointer(
            self.config.endpoint_silence_ms, self.config.max_utterance_seconds
        )
        captured: list[np.ndarray] = list(self._preroll)
        self._preroll.clear()

        while True:
            frame = await self._queue.get()
            captured.append(frame)
            done = False
            for sub in self._split_vad_frames(frame):
                if endpointer.feed(self._voiced(sub)):
                    done = True
                    break
            if done:
                break

        if not endpointer.heard_speech:
            log.info("wake word fired but nothing was said")
            return

        audio = np.concatenate(captured)
        text = await asyncio.to_thread(self._transcribe, audio)
        if text:
            log.info("heard: %s", text)
            self._on_utterance(text)
        else:
            log.info("nothing transcribable")

    def _transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._whisper.transcribe(
            audio,
            language="en",
            beam_size=1,           # greedy: faster, and plenty for short commands
            vad_filter=False,      # already endpointed
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()
