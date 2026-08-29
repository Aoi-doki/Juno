"""Speech synthesis with Kokoro-82M, streamed sentence by sentence.

Kokoro synthesises far faster than real time on a laptop CPU, but a long reply
still takes a noticeable moment to render in one go. Splitting on sentences and
playing the first while the second renders means she starts talking almost
immediately, which is most of what makes a voice assistant feel responsive.

Playback is interruptible at a sentence boundary and mid-sentence, so barge-in
cuts her off the way a person would be cut off.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Iterator

import numpy as np

from juno_laptop.config import ClientConfig

log = logging.getLogger(__name__)

# Split after . ! ? … and newlines, keeping the punctuation. Abbreviations that
# would cause a false split mid-sentence are rare in spoken replies and cost
# only a slightly early breath, so this stays a regex rather than a parser.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")

# Below this, a fragment isn't worth a separate synthesis pass — it gets glued
# onto the previous sentence instead, avoiding a stutter on "Yes. OK."
_MIN_CHUNK_CHARS = 24


def split_sentences(text: str) -> list[str]:
    """Break a reply into speakable chunks.

    Short fragments are merged into a neighbour so playback never stutters on
    one-word sentences — backwards where there is a previous chunk, forwards
    otherwise, which is the common case: replies open with "Morning." or "Yes."
    far more often than they end with one.
    """
    text = " ".join(text.split())
    if not text:
        return []

    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]

    chunks: list[str] = []
    carry = ""  # a short fragment waiting for the sentence that follows it
    for part in parts:
        if carry:
            part = f"{carry} {part}"
            carry = ""
        if len(part) < _MIN_CHUNK_CHARS:
            if chunks:
                chunks[-1] = f"{chunks[-1]} {part}"
            else:
                carry = part
            continue
        chunks.append(part)

    # A short fragment with nothing after it: the whole reply was tiny.
    if carry:
        chunks.append(carry)
    return chunks


class Speaker:
    """Owns the output device and the Kokoro model.

    ``interrupt`` is an ``asyncio.Event`` the listener sets when it hears the
    user talk over her.
    """

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.interrupt = asyncio.Event()
        self._kokoro = None
        self._speaking = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    def _load(self):
        if self._kokoro is not None:
            return self._kokoro
        from kokoro_onnx import Kokoro  # imported late: ~1s and only when needed

        model = self.config.model_dir / "kokoro-v1.0.onnx"
        voices = self.config.model_dir / "voices-v1.0.bin"
        for path in (model, voices):
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} is missing — run scripts/fetch-models.sh first"
                )
        log.info("loading Kokoro from %s", model)
        self._kokoro = Kokoro(str(model), str(voices))
        return self._kokoro

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        """Blocking synthesis of one chunk. Run via ``to_thread``."""
        kokoro = self._load()
        samples, rate = kokoro.create(
            text, voice=self.config.kokoro_voice, speed=self.config.kokoro_speed, lang="en-us"
        )
        return samples, rate

    async def say(self, text: str) -> bool:
        """Speak a whole reply. Returns False if it was interrupted.

        Each sentence is synthesised while the previous one plays, so the gap
        between them is inaudible after the first.
        """
        chunks = split_sentences(text)
        if not chunks:
            return True

        self.interrupt.clear()
        self._speaking = True
        try:
            pending = asyncio.create_task(asyncio.to_thread(self.synth, chunks[0]))
            for index, _ in enumerate(chunks):
                samples, rate = await pending
                if index + 1 < len(chunks):
                    pending = asyncio.create_task(
                        asyncio.to_thread(self.synth, chunks[index + 1])
                    )
                if not await self._play(samples, rate):
                    # Drain the lookahead so a cancelled task doesn't warn.
                    if index + 1 < len(chunks):
                        pending.cancel()
                    return False
            return True
        finally:
            self._speaking = False

    async def _play(self, samples: np.ndarray, rate: int) -> bool:
        """Play one chunk, checking for interruption as it goes."""
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        done = loop.create_future()
        position = 0
        # ~50 ms blocks: small enough that a barge-in stops her promptly,
        # large enough not to underrun.
        block = max(1, rate // 20)

        def callback(outdata, frames, _time, status):  # noqa: ANN001
            nonlocal position
            if status:
                log.debug("output status: %s", status)
            end = position + frames
            chunk = samples[position:end]
            outdata[: len(chunk), 0] = chunk
            if len(chunk) < frames:
                outdata[len(chunk):, 0] = 0
                raise sd.CallbackStop
            position = end

        stream = sd.OutputStream(
            samplerate=rate,
            channels=1,
            dtype="float32",
            blocksize=block,
            callback=callback,
            finished_callback=lambda: loop.call_soon_threadsafe(
                lambda: done.done() or done.set_result(None)
            ),
        )
        with stream:
            while not done.done():
                if self.interrupt.is_set():
                    log.info("interrupted mid-sentence")
                    return False
                await asyncio.sleep(0.02)
        return not self.interrupt.is_set()

    def stop(self) -> None:
        self.interrupt.set()


def available_voices() -> Iterator[str]:
    """The Kokoro presets worth auditioning.

    The full set is larger; these are the ones that hold up for a long reply.
    ``a`` is American, ``b`` British; ``f`` female, ``m`` male.
    """
    yield from (
        "af_heart",     # warm, the default
        "af_bella",     # brighter, more clipped
        "af_nicole",    # softer, breathier
        "af_sarah",
        "af_sky",
        "bf_emma",      # British, calm and dry
        "bf_isabella",
        "am_michael",
        "am_puck",
        "bm_george",
    )
