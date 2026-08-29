"""Play the same lines in every Kokoro voice so you can pick one by ear.

Run this before anything else — the voice is the thing you'll hear thousands of
times, and reading a list of preset names tells you nothing useful about it.

    juno-audition                 # every voice, both lines
    juno-audition --save out/     # write WAVs instead of playing
    juno-audition -v af_heart     # just one
"""

from __future__ import annotations

import argparse
import logging
import sys
import wave
from pathlib import Path

import numpy as np

from juno_laptop.config import ClientConfig
from juno_laptop.speak import Speaker, available_voices

# One calm line and one nudge: a voice that sounds pleasant reading the weather
# can sound insufferable telling you to put your phone down, and the second is
# what you'll actually hear most.
LINES = [
    "Morning. You've got a call at eleven, and nothing before it.",
    "That's forty minutes on Reddit. You said you were writing the report.",
]


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((pcm * 32767).astype(np.int16).tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Audition Kokoro voices for Juno.")
    parser.add_argument("-v", "--voice", action="append", help="Only this voice. Repeatable.")
    parser.add_argument("--save", type=Path, help="Write WAVs to this directory instead of playing.")
    parser.add_argument("--text", help="Say this instead of the built-in lines.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    config = ClientConfig(token="unused-for-audition")
    speaker = Speaker(config)

    voices = args.voice or list(available_voices())
    lines = [args.text] if args.text else LINES

    for voice in voices:
        config.kokoro_voice = voice
        print(f"\n=== {voice} ===", flush=True)
        for index, line in enumerate(lines, start=1):
            print(f"  {line}", flush=True)
            try:
                samples, rate = speaker.synth(line)
            except FileNotFoundError as exc:
                print(f"\n{exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 - one bad preset shouldn't end the run
                print(f"  (failed: {exc})", file=sys.stderr)
                continue

            if args.save:
                path = args.save / f"{voice}-{index}.wav"
                _write_wav(path, samples, rate)
                print(f"  -> {path}", flush=True)
            else:
                import sounddevice as sd

                sd.play(samples, rate)
                sd.wait()

    if not args.save:
        print("\nSet your pick as kokoro_voice in client.yaml (and the brain's config.yaml).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
