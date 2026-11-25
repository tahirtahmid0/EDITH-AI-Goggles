"""Short-term visual identity reconciliation for EDITH.

ByteTrack IDs are temporary. This module assigns session-scoped visual identity
IDs and cautiously reconnects new tracks to recently observed identities.
It uses geometry and motion only; it does not claim biometric/person recognition.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class IdentityState:
    identity_id: str
    class_name: str
    center: tuple[float, float]
    bbox: tuple[float, float, float, float]
    last_seen: float
    velocity: tuple[float, float]
    last_track_id: int


class IdentityManager:
    """Associate short tracking interruptions without forcing weak matches."""

    def __init__(
        self,
        strong_threshold: float = 0.68,
        possible_threshold: float = 0.45,
        reid_window_seconds: float = 6.0,
    ) -> None:
        self.strong_threshold = strong_threshold
        self.possible_threshold = possible_threshold
        self.reid_window_seconds = reid_window_seconds
        self.identities: dict[str, IdentityState] = {}
        self.track_to_identity: dict[int, str] = {}
        self.next_identity_number = 1
        self.seen_this_frame: set[str] = set()

    def begin_frame(self) -> None:
        self.seen_this_frame.clear()

    @staticmethod
    def _iou(
        a: tuple[float, float, float, float],
        b: tuple[float, float, float, float],
    ) -> float:
        left, top = max(a[0], b[0]), max(a[1], b[1])
        right, bottom = min(a[2], b[2]), min(a[3], b[3])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
        area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
        union = area_a + area_b - intersection
        return intersection / union if union > 0 else 0.0

    def _score(
        self,
        state: IdentityState,
        center: tuple[float, float],
        bbox: tuple[float, float, float, float],
        now: float,
        frame_diagonal: float,
    ) -> float:
        elapsed = max(0.0, now - state.last_seen)
        predicted = (
            state.center[0] + state.velocity[0] * min(elapsed, 1.5),
            state.center[1] + state.velocity[1] * min(elapsed, 1.5),
        )
        distance = math.dist(predicted, center)
        normalized_distance = distance / max(frame_diagonal, 1.0)

        # Reject geometrically implausible jumps before scoring.
        if normalized_distance > 0.35:
            return 0.0

        position_score = math.exp(-normalized_distance / 0.18)
        overlap_score = self._iou(state.bbox, bbox)

        old_area = max(1.0, (state.bbox[2] - state.bbox[0]) * (state.bbox[3] - state.bbox[1]))
        new_area = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        size_ratio = min(old_area, new_area) / max(old_area, new_area)

        recency_score = max(0.0, 1.0 - elapsed / self.reid_window_seconds)
        return (
            0.55 * position_score
            + 0.25 * overlap_score
            + 0.10 * size_ratio
            + 0.10 * recency_score
        )

    def _new_identity(
        self,
        class_name: str,
        center: tuple[float, float],
        bbox: tuple[float, float, float, float],
        now: float,
        track_id: int,
    ) -> IdentityState:
        identity_id = f"V{self.next_identity_number:04d}"
        self.next_identity_number += 1
        state = IdentityState(
            identity_id=identity_id,
            class_name=class_name,
            center=center,
            bbox=bbox,
            last_seen=now,
            velocity=(0.0, 0.0),
            last_track_id=track_id,
        )
        self.identities[identity_id] = state
        self.track_to_identity[track_id] = identity_id
        self.seen_this_frame.add(identity_id)
        return state

    def assign(
        self,
        observation: dict[str, Any],
        frame_size: tuple[int, int],
        now: float,
    ) -> dict[str, Any]:
        track_id = int(observation["track_id"])
        class_name = str(observation["class_name"])
        center = tuple(float(v) for v in observation["center"])
        bbox = tuple(float(v) for v in observation["bbox"])
        frame_diagonal = math.hypot(frame_size[0], frame_size[1])

        # Retain identities briefly after the tracker stops seeing them.
        expired = [
            identity_id
            for identity_id, state in self.identities.items()
            if now - state.last_seen > self.reid_window_seconds
        ]
        for identity_id in expired:
            del self.identities[identity_id]
        self.track_to_identity = {
            tid: iid for tid, iid in self.track_to_identity.items()
            if iid in self.identities
        }

        identity_id = self.track_to_identity.get(track_id)
        state = self.identities.get(identity_id) if identity_id else None

        if state is not None and state.class_name == class_name:
            dt = max(0.001, now - state.last_seen)
            measured_velocity = (
                (center[0] - state.center[0]) / dt,
                (center[1] - state.center[1]) / dt,
            )
            state.velocity = (
                0.65 * state.velocity[0] + 0.35 * measured_velocity[0],
                0.65 * state.velocity[1] + 0.35 * measured_velocity[1],
            )
            state.center, state.bbox = center, bbox
            state.last_seen, state.last_track_id = now, track_id
            self.seen_this_frame.add(state.identity_id)
            return {
                "visual_identity_id": state.identity_id,
                "identity_match": "same_track",
                "identity_score": 1.0,
                "possible_match_to": None,
                "identity_created": False,
            }

        # A new ByteTrack ID may be the same object after a brief track break.
        candidates: list[tuple[float, IdentityState]] = []
        for candidate in self.identities.values():
            if candidate.class_name != class_name:
                continue
            if candidate.identity_id in self.seen_this_frame:
                continue
            elapsed = now - candidate.last_seen
            if elapsed < 0 or elapsed > self.reid_window_seconds:
                continue
            score = self._score(candidate, center, bbox, now, frame_diagonal)
            candidates.append((score, candidate))

        best_score, best_state = max(candidates, key=lambda item: item[0], default=(0.0, None))

        if best_state is not None and best_score >= self.strong_threshold:
            old_track_id = best_state.last_track_id
            dt = max(0.001, now - best_state.last_seen)
            measured_velocity = (
                (center[0] - best_state.center[0]) / dt,
                (center[1] - best_state.center[1]) / dt,
            )
            best_state.velocity = (
                0.65 * best_state.velocity[0] + 0.35 * measured_velocity[0],
                0.65 * best_state.velocity[1] + 0.35 * measured_velocity[1],
            )
            best_state.center, best_state.bbox = center, bbox
            best_state.last_seen, best_state.last_track_id = now, track_id
            self.track_to_identity[track_id] = best_state.identity_id
            self.seen_this_frame.add(best_state.identity_id)
            return {
                "visual_identity_id": best_state.identity_id,
                "identity_match": "reconnected",
                "identity_score": round(best_score, 3),
                "possible_match_to": None,
                "previous_track_id": old_track_id,
                "identity_created": False,
            }

        possible_match_to = (
            best_state.identity_id
            if best_state is not None and best_score >= self.possible_threshold
            else None
        )
        new_state = self._new_identity(class_name, center, bbox, now, track_id)
        return {
            "visual_identity_id": new_state.identity_id,
            "identity_match": "uncertain_new" if possible_match_to else "new",
            "identity_score": round(best_score, 3) if possible_match_to else None,
            "possible_match_to": possible_match_to,
            "identity_created": True,
        }

    def identity_for_track(self, track_id: int) -> str | None:
        return self.track_to_identity.get(track_id)
