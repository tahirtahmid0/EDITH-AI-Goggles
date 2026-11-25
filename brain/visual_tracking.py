"""EDITH local visual tracking engine.

Runs YOLO11 + ByteTrack against the ESP32-CAM MJPEG stream.
Writes event records to a JSONL file and does not touch the existing SQLite memory.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

import torch
from PIL import Image
from ultralytics import YOLO

# Support both launch styles:
#   python -m brain.visual_tracking
#   python brain/visual_tracking.py (including IDE Run configurations)
if __package__:
    from .gemini_live import get_camera_stream_url, iter_mjpeg_frames
    from .identity_manager import IdentityManager
else:
    from gemini_live import get_camera_stream_url, iter_mjpeg_frames
    from identity_manager import IdentityManager


BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "models" / "yolo" / "yolo11s.pt"
EVENTS_PATH = BASE_DIR / "memory" / "visual_tracking_events.jsonl"

# Start conservatively on the M1; increase only after checking performance.
TRACKING_FPS = 3.0
IMAGE_SIZE = 640
CONFIDENCE = 0.15
MOVEMENT_THRESHOLD_PX = 45.0
DISAPPEAR_AFTER_SECONDS = 4.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class VisualTracker:
    """Convert YOLO/ByteTrack results into simple, timestamped events."""

    def __init__(self, model: YOLO, events_path: Path = EVENTS_PATH):
        self.model = model
        self.events_path = events_path
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.active: dict[int, dict[str, Any]] = {}
        self.identity_manager = IdentityManager()
        self.timings: list[dict[str, float]] = []

    def record(self, event_type: str, track_id: int, **details: Any) -> None:
        event = {
            "timestamp": utc_now(),
            "event": event_type,
            "track_id": track_id,
            **details,
        }
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")

        print(
            f"[VISUAL MEMORY] {event_type} | "
            f"ID {track_id} | {details.get('class_name', 'object')}"
        )

    def process_jpeg(self, jpeg: bytes) -> list[dict[str, Any]]:
        frame_started = time.perf_counter()
        decode_started = time.perf_counter()
        with Image.open(__import__("io").BytesIO(jpeg)) as source:
            image = source.convert("RGB")
        decode_ms = (time.perf_counter() - decode_started) * 1000

        model_started = time.perf_counter()
        results = self.model.track(
            source=image,
            persist=True,
            tracker="bytetrack.yaml",
            device="mps" if torch.backends.mps.is_available() else "cpu",
            imgsz=IMAGE_SIZE,
            conf=CONFIDENCE,
            verbose=False,
        )
        model_ms = (time.perf_counter() - model_started) * 1000
        result = results[0]
        boxes = result.boxes
        bookkeeping_started = time.perf_counter()
        now = time.monotonic()
        seen_ids: set[int] = set()
        observations: list[dict[str, Any]] = []
        self.identity_manager.begin_frame()

        if boxes is not None and boxes.id is not None:
            coordinates = boxes.xyxy.cpu().tolist()
            track_ids = boxes.id.int().cpu().tolist()
            confidences = boxes.conf.cpu().tolist()
            class_ids = boxes.cls.int().cpu().tolist()

            for bbox, raw_id, confidence, class_id in zip(
                coordinates, track_ids, confidences, class_ids
            ):
                track_id = int(raw_id)
                class_name = self.model.names[int(class_id)]
                x1, y1, x2, y2 = [float(value) for value in bbox]
                center = [(x1 + x2) / 2, (y1 + y2) / 2]
                seen_ids.add(track_id)

                previous = self.active.get(track_id)
                observation = {
                    "track_id": track_id,
                    "class_name": class_name,
                    "confidence": round(float(confidence), 3),
                    "bbox": [round(value, 1) for value in (x1, y1, x2, y2)],
                    "center": [round(center[0], 1), round(center[1], 1)],
                }
                identity = self.identity_manager.assign(observation, image.size, now)
                observation.update(identity)
                observations.append(observation)

                if identity["identity_match"] == "reconnected":
                    self.record(
                        "identity_reassociated",
                        track_id,
                        class_name=class_name,
                        visual_identity_id=identity["visual_identity_id"],
                        previous_track_id=identity.get("previous_track_id"),
                        match_score=identity["identity_score"],
                    )
                elif identity["identity_match"] == "uncertain_new":
                    self.record(
                        "identity_possible_match",
                        track_id,
                        class_name=class_name,
                        visual_identity_id=identity["visual_identity_id"],
                        possible_match_to=identity["possible_match_to"],
                        match_score=identity["identity_score"],
                        note="Kept as a separate identity because the match was not strong enough",
                    )

                if previous is None:
                    self.active[track_id] = {
                        "class_name": class_name,
                        "last_seen": now,
                        "last_event_center": center,
                    }
                    self.record(
                        "object_appeared",
                        track_id,
                        class_name=class_name,
                        confidence=observation["confidence"],
                        center=observation["center"],
                        bbox=observation["bbox"],
                        visual_identity_id=identity["visual_identity_id"],
                        identity_match=identity["identity_match"],
                    )
                else:
                    previous["last_seen"] = now
                    previous["class_name"] = class_name
                    anchor = previous["last_event_center"]
                    distance = (
                        (center[0] - anchor[0]) ** 2
                        + (center[1] - anchor[1]) ** 2
                    ) ** 0.5

                    if distance >= MOVEMENT_THRESHOLD_PX:
                        self.record(
                            "object_moved",
                            track_id,
                            class_name=class_name,
                            from_center=[round(anchor[0], 1), round(anchor[1], 1)],
                            to_center=observation["center"],
                            displacement_px=round(distance, 1),
                            visual_identity_id=identity["visual_identity_id"],
                        )
                        previous["last_event_center"] = center

        # A short detection gap is not treated as disappearance. ByteTrack may
        # continue the same track when the object is detected again.
        expired = [
            track_id
            for track_id, state in self.active.items()
            if track_id not in seen_ids
            and now - state["last_seen"] >= DISAPPEAR_AFTER_SECONDS
        ]
        for track_id in expired:
            state = self.active.pop(track_id)
            self.record(
                "object_disappeared",
                track_id,
                class_name=state["class_name"],
                last_seen_seconds_ago=round(now - state["last_seen"], 1),
                visual_identity_id=self.identity_manager.identity_for_track(track_id),
            )

        bookkeeping_ms = (time.perf_counter() - bookkeeping_started) * 1000
        total_ms = (time.perf_counter() - frame_started) * 1000
        self.timings.append({
            "decode_ms": decode_ms,
            "model_ms": model_ms,
            "bookkeeping_ms": bookkeeping_ms,
            "total_ms": total_ms,
        })
        return observations


def run() -> None:
    if not MODEL_PATH.is_file():
        raise FileNotFoundError(f"YOLO weights not found: {MODEL_PATH}")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    stream_url = get_camera_stream_url()
    stop_event = Event()
    interval = 1.0 / TRACKING_FPS

    def request_stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print("=" * 48)
    print("       EDITH - LOCAL VISUAL TRACKING")
    print("=" * 48)
    print(f"Device: {device}")
    print(f"Model: {MODEL_PATH}")
    print(f"Camera: {stream_url}")
    print(f"Target tracking rate: {TRACKING_FPS:.1f} FPS")
    print(f"Event log: {EVENTS_PATH}")
    print("Press Ctrl+C to stop.\n")

    model = YOLO(str(MODEL_PATH))
    tracker = VisualTracker(model)
    frame_count = 0
    processed_count = 0
    next_process = 0.0
    started = time.monotonic()

    try:
        for jpeg in iter_mjpeg_frames(stream_url, stop_event):
            if stop_event.is_set():
                break

            now = time.monotonic()
            if now < next_process:
                continue

            next_process = now + interval
            observations = tracker.process_jpeg(jpeg)
            frame_count += 1
            processed_count += 1

            summary = ", ".join(
                f"ID {item['track_id']} {item['class_name']}"
                for item in observations
            )
            print(
                f"[FRAME {processed_count}] "
                f"{summary if summary else 'No tracked objects'}"
            )

    finally:
        stop_event.set()
        elapsed = max(time.monotonic() - started, 0.001)
        print("\n[EDITH TRACKING] Stopped.")
        print(f"Processed frames: {processed_count}")
        print(f"Average processing rate: {processed_count / elapsed:.2f} FPS")
        if tracker.timings:
            count = len(tracker.timings)
            for key, label in (
                ("decode_ms", "JPEG decode"),
                ("model_ms", "YOLO + ByteTrack"),
                ("bookkeeping_ms", "Event processing + logging"),
                ("total_ms", "Total frame processing"),
            ):
                average = sum(sample[key] for sample in tracker.timings) / count
                print(f"Average {label} time: {average:.1f} ms")
        print(f"Event log: {EVENTS_PATH}")
        del model


if __name__ == "__main__":
    run()
