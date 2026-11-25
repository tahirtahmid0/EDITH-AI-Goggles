"""EDITH experimental Gemini Live vision backend.

This module is deliberately standalone: it does not import or modify the
existing YOLO11, Moondream, audio, or SQLite pipelines.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
FRAME_INTERVAL_SECONDS = 1.05  # Live API video input is limited to 1 FPS.
JPEG_MAX_BUFFER_BYTES = 2_000_000

SYSTEM_INSTRUCTION = """You are EDITH's live visual understanding system.
You receive JPEG frames from a wearable camera. Describe only what is
reasonably visible, distinguish observations from guesses, and pay attention
to changes in object positions and the scene over time. When the user asks a
question, answer using the most recent frames and relevant visual context.
Do not claim to see details that are too small, obscured, or uncertain."""


def get_camera_stream_url() -> str:
    """Use an explicit stream URL, or derive it from EDITH's existing capture URL."""
    stream_url = os.getenv("CAMERA_STREAM_URL", "").strip()
    if stream_url:
        return stream_url

    capture_url = os.getenv("CAMERA_URL", "").strip()
    if not capture_url:
        raise RuntimeError(
            "Set CAMERA_STREAM_URL in .env, or set CAMERA_URL so EDITH can "
            "derive the ESP32-CAM stream address."
        )

    parsed = urlsplit(capture_url)
    if not parsed.scheme or not parsed.hostname:
        raise RuntimeError("CAMERA_URL is not a valid HTTP URL.")

    # Preserve the host, but use the ESP32-CAM MJPEG server on port 81.
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:81"
    return urlunsplit((parsed.scheme, netloc, "/stream", "", ""))


def iter_mjpeg_frames(url: str, stop_event: threading.Event):
    """Yield complete JPEG frames from a multipart MJPEG HTTP response.

    JPEG marker extraction avoids depending on a particular multipart boundary
    string, while keeping memory bounded if the connection becomes malformed.
    """
    with requests.get(
        url,
        stream=True,
        timeout=(5, 20),
        headers={"Cache-Control": "no-cache"},
    ) as response:
        response.raise_for_status()
        buffer = bytearray()

        for chunk in response.iter_content(chunk_size=4096):
            if stop_event.is_set():
                return
            if not chunk:
                continue

            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    # Keep a possible split marker at the end of the buffer.
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break

                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    if len(buffer) > JPEG_MAX_BUFFER_BYTES:
                        raise RuntimeError("MJPEG frame exceeded the safety buffer limit.")
                    break

                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                yield frame


def camera_reader(url: str, frames: queue.Queue, stop_event: threading.Event):
    """Continuously read MJPEG and retain only the newest frame."""
    print(f"[EDITH GEMINI] Connecting to camera stream: {url}")
    while not stop_event.is_set():
        try:
            for frame in iter_mjpeg_frames(url, stop_event):
                if stop_event.is_set():
                    break
                # Drop stale frames so Gemini always receives a recent image.
                try:
                    frames.put_nowait(frame)
                except queue.Full:
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        frames.put_nowait(frame)
                    except queue.Full:
                        pass
        except Exception as exc:
            if not stop_event.is_set():
                print(f"[EDITH GEMINI] Camera stream error: {exc}")
                stop_event.wait(2.0)

    print("[EDITH GEMINI] Camera reader stopped.")


async def send_camera_frames(
    session,
    frames: queue.Queue,
    stop_event: threading.Event,
    camera_ready: asyncio.Event,
):
    """Send the latest camera JPEG at a safe, approximately 1 FPS cadence."""
    loop = asyncio.get_running_loop()
    sent = 0
    next_send = loop.time()

    while not stop_event.is_set():
        try:
            frame = await asyncio.to_thread(frames.get, True, 2.0)
        except queue.Empty:
            continue

        delay = next_send - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        await session.send_realtime_input(
            video=types.Blob(data=frame, mime_type="image/jpeg")
        )
        sent += 1
        if sent <= 3 or sent % 30 == 0:
            print(f"[EDITH GEMINI] Sent {sent} camera frames.")

        # Do not let the user ask visual questions until several real frames
        # have successfully been sent to Gemini.
        if sent >= 3 and not camera_ready.is_set():
            camera_ready.set()
            print("[EDITH GEMINI] Camera feed confirmed; visual questions enabled.")

        next_send = loop.time() + FRAME_INTERVAL_SECONDS


async def send_user_text(
    session,
    stop_event: threading.Event,
    camera_ready: asyncio.Event,
):
    """Read typed questions only after camera frames reach the Live session."""
    print("\n[EDITH GEMINI] Waiting for the camera feed to initialise...")

    while not stop_event.is_set() and not camera_ready.is_set():
        try:
            await asyncio.wait_for(camera_ready.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            continue

    if stop_event.is_set():
        return

    print("\n[EDITH GEMINI] Live vision is ready.")
    print("Ask a question about the camera view. Type 'exit' to stop.\n")

    while not stop_event.is_set():
        try:
            question = await asyncio.to_thread(input, "[YOU] ")
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            return

        question = question.strip()
        if not question:
            continue
        if question.lower() in {"exit", "quit", "stop"}:
            stop_event.set()
            return

        await session.send_realtime_input(text=question)


async def receive_gemini_responses(session, stop_event: threading.Event):
    """Keep receiving Live API events across multiple completed response turns."""
    while not stop_event.is_set():
        # The SDK receive iterator can finish at the end of a response turn.
        # Re-enter it so the same Live session remains available for the next
        # question instead of treating one answer as session completion.
        async for response in session.receive():
            content = response.server_content
            if not content:
                continue

            # Live models return audio; request and display its text transcription.
            if content.output_transcription and content.output_transcription.text:
                print(
                    f"\n[EDITH / GEMINI] {content.output_transcription.text}",
                    end="",
                    flush=True,
                )

            if content.turn_complete:
                print("\n[EDITH GEMINI] Response complete.\n", flush=True)

            if stop_event.is_set():
                return

        if not stop_event.is_set():
            # Avoid a tight loop if the receive iterator ends between turns.
            await asyncio.sleep(0.05)


async def run_live_vision():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add it to your local .env file; "
            "never commit the key to GitHub."
        )

    stream_url = get_camera_stream_url()
    frame_queue: queue.Queue = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    reader = threading.Thread(
        target=camera_reader,
        args=(stream_url, frame_queue, stop_event),
        name="edith-gemini-camera",
        daemon=True,
    )
    reader.start()

    client = genai.Client(
        api_key=api_key,
        # Gemini Live API is currently available on v1beta, while google-genai 3.x
        # defaults the Gemini Developer API to v1.
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(
            parts=[types.Part(text=SYSTEM_INSTRUCTION)]
        ),
    )

    print(f"[EDITH GEMINI] Connecting to model: {MODEL}")
    print("[EDITH GEMINI] Video input is limited to approximately 1 FPS.")

    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            print("[EDITH GEMINI] Live API session connected.")
            camera_ready = asyncio.Event()
            tasks = [
                asyncio.create_task(
                    send_camera_frames(session, frame_queue, stop_event, camera_ready)
                ),
                asyncio.create_task(
                    send_user_text(session, stop_event, camera_ready)
                ),
                asyncio.create_task(receive_gemini_responses(session, stop_event)),
            ]

            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                exception = task.exception()
                if exception:
                    raise exception
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        stop_event.set()
        reader.join(timeout=3.0)
        await client.aio.aclose()
        print("[EDITH GEMINI] Session closed.")


def main():
    print("========================================")
    print("       EDITH - GEMINI LIVE VISION")
    print("       Experimental backend")
    print("========================================")
    try:
        asyncio.run(run_live_vision())
    except KeyboardInterrupt:
        print("\n[EDITH GEMINI] Shutdown requested.")
    except Exception:
        print("\n[EDITH GEMINI ERROR]")
        traceback.print_exc()


if __name__ == "__main__":
    main()
