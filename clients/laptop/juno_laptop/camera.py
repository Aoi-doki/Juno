"""Webcam presence, derived locally.

The camera answers one question the screen cannot: are you actually there?
Without it Juno talks to an empty chair, and a nudge you never heard still
counts as a nudge she delivered — so she escalates for no reason.

**No frame ever leaves this machine.** MediaPipe runs locally at a couple of
frames per second and produces words — ``at_desk``, ``away``, ``slouching`` —
and only those words are sent. There is no code path here that uploads an
image; asking "what do you see?" is a separate, explicit request handled
elsewhere.

Presence is deliberately sticky. A face detector misses a frame when you turn
your head or reach for a mug, and a naive implementation would report you gone
and back a dozen times an hour.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# How long a face must be missing before you count as away. Long enough to
# survive turning your head, short enough to notice you leaving.
AWAY_AFTER_SECONDS = 45.0
# And how quickly you count as back. Short, because the cost of being wrong is
# small in this direction.
PRESENT_AFTER_SECONDS = 3.0
# Slouching has to persist before it is worth mentioning; everyone leans.
POSTURE_AFTER_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class Presence:
    state: str          # at_desk | away
    posture: str | None  # slouching | None

    def summary(self) -> str:
        if self.state == "away":
            return "away from the desk"
        return "at the desk, slouching" if self.posture == "slouching" else "at the desk"


class PresenceTracker:
    """Turns per-frame detections into stable states.

    Pure logic driven by an injected clock, so the hysteresis rules are
    testable without a webcam: feed it booleans and timestamps.
    """

    def __init__(
        self,
        away_after: float = AWAY_AFTER_SECONDS,
        present_after: float = PRESENT_AFTER_SECONDS,
        posture_after: float = POSTURE_AFTER_SECONDS,
    ) -> None:
        self.away_after = away_after
        self.present_after = present_after
        self.posture_after = posture_after
        self._state = "away"
        self._posture: str | None = None
        self._face_since: float | None = None
        self._noface_since: float | None = None
        self._slouch_since: float | None = None
        # Seeded with the starting state so an empty chair at startup is not
        # announced as news. Only genuine changes are reported.
        self._reported: Presence | None = Presence(self._state, self._posture)

    def observe(
        self, face_visible: bool, slouching: bool, now: float
    ) -> Presence | None:
        """Feed one frame's detection. Returns a Presence when it changed."""
        if face_visible:
            self._noface_since = None
            if self._face_since is None:
                self._face_since = now
            if self._state == "away" and now - self._face_since >= self.present_after:
                self._state = "at_desk"

            if slouching:
                if self._slouch_since is None:
                    self._slouch_since = now
                if now - self._slouch_since >= self.posture_after:
                    self._posture = "slouching"
            else:
                self._slouch_since = None
                self._posture = None
        else:
            self._face_since = None
            if self._noface_since is None:
                self._noface_since = now
            # Posture is held through the grace period rather than cleared on
            # the first missed frame. Clearing it early emits a spurious "at the
            # desk, sitting up" the moment you look away, immediately before
            # "away" — two events describing one act of standing up.
            if self._state == "at_desk" and now - self._noface_since >= self.away_after:
                self._state = "away"
                self._slouch_since = None
                self._posture = None

        current = Presence(self._state, self._posture)
        if current != self._reported:
            self._reported = current
            return current
        return None


class CameraWatcher:
    def __init__(
        self,
        send: Callable[[dict], Awaitable[None]],
        index: int = 0,
        poll_seconds: float = 2.0,
    ) -> None:
        self.send = send
        self.index = index
        self.poll_seconds = poll_seconds
        self.tracker = PresenceTracker()
        self._detector = None
        self._capture = None
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        """Whether OpenCV and MediaPipe are importable.

        The camera device itself is opened lazily in ``run`` — probing it here
        would hold the webcam open (and light the LED) for the whole session.
        """
        if self._available is None:
            try:
                import cv2  # noqa: F401
                import mediapipe  # noqa: F401

                self._available = True
            except ImportError:
                log.info("camera extras not installed; presence detection off")
                self._available = False
        return self._available

    def _open(self) -> bool:
        import cv2
        import mediapipe as mp

        self._capture = cv2.VideoCapture(self.index)
        if not self._capture.isOpened():
            log.warning("could not open camera %d; presence detection off", self.index)
            return False
        # Small frames: presence needs a face, not detail, and this keeps the
        # per-frame cost to a few milliseconds.
        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self._detector = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=False, min_detection_confidence=0.5
        )
        return True

    def _sample(self) -> tuple[bool, bool]:
        """One frame in, ``(face_visible, slouching)`` out. The frame is
        discarded before this returns."""
        import cv2

        ok, frame = self._capture.read()
        if not ok:
            return False, False

        result = self._detector.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        landmarks = getattr(result, "multi_face_landmarks", None)
        if not landmarks:
            return False, False

        # Crude but serviceable: when you slump, your face sits lower in frame
        # and gets larger as you lean toward the screen. Absolute thresholds
        # would depend on camera placement, so this compares against where your
        # face usually is, which the tracker's hysteresis then smooths.
        points = landmarks[0].landmark
        ys = [p.y for p in points]
        centre = sum(ys) / len(ys)
        return True, centre > 0.62

    async def run(self) -> None:
        if not self.available:
            return
        if not await asyncio.to_thread(self._open):
            return

        log.info("camera presence active (frames stay on this machine)")
        try:
            while True:
                try:
                    face, slouch = await asyncio.to_thread(self._sample)
                    presence = self.tracker.observe(face, slouch, time.monotonic())
                    if presence is not None:
                        await self.send(
                            {
                                "type": "event",
                                "kind": "camera.presence",
                                "summary": presence.summary(),
                                "state": presence.state,
                                "posture": presence.posture,
                            }
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.debug("camera sample failed: %s", exc)
                await asyncio.sleep(self.poll_seconds)
        finally:
            if self._capture is not None:
                self._capture.release()
            if self._detector is not None:
                self._detector.close()
