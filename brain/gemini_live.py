"""EDITH's integrated Gemini Live assistant.

Streams the newest camera frame to Gemini Live at the API's 1 FPS limit.
Text commands are sent through the same persistent session. Gemini returns
native audio plus an output transcript; audio playback is handled by audio.py.
"""

from __future__ import annotations

import asyncio
import io
import os
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
from PIL import Image
from google import genai
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live").strip()
FRAME_INTERVAL_SECONDS = 1.05
JPEG_MAX_BUFFER_BYTES = 2_000_000

SYSTEM_INSTRUCTION = """You are EDITH, a practical, observant wearable AI assistant.
You receive a continuing camera feed and questions from the wearer. Use the
most recently received camera frames as visual evidence.

IMPORTANT RESPONSE RULES:
- Do not volunteer descriptions of the camera feed during greetings or
  ordinary conversation. Use visual information only when it is directly
  relevant to the user's question or the user asks about what the camera sees.
- A greeting such as "hi" or "hello" should receive a natural greeting, not
  a description of the surroundings.
- Answer the exact question asked. Do not merely describe nearby objects while
  ignoring the requested detail.
- Put the direct answer in your first sentence.
- If asked for a colour, state the visible colour. If asked for a count, give
  the count. If asked for a location, describe its position in the image.
- If asked which object is closest to the camera, identify the single object
  that appears nearest. Do not list several objects unless the user asks.
- Do not claim precise physical distance or depth from a single image when it
  cannot be established. Say "appears closest" when appropriate.
- Describe only details that are clearly visible. Never invent objects,
  locations, people, actions, colours, or context.
- If a requested detail is unclear, say exactly what is uncertain rather than
  refusing the entire question.
- Keep answers concise, natural, and conversational. Usually one or two
  sentences are enough.
- Remember the conversation within this live session, but treat the newest
  camera frames as the source of truth for current-scene questions.
- Do not claim to have performed actions or accessed memories unless the
  user-facing system has actually provided that information."""


def get_camera_stream_url() -> str:
    """Return the ESP32-CAM's continuous MJPEG stream endpoint."""
    camera_url = os.getenv("CAMERA_URL", "").strip()
    if not camera_url:
        raise RuntimeError("Set CAMERA_URL in .env, or set EDITH_IMAGE_PATH to use a local test image.")
    parsed = urlsplit(camera_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("CAMERA_URL is not a valid HTTP URL.")
    port = parsed.port
    if port is None or port == 80:
        port = 81
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit((parsed.scheme, f"{host}:{port}", "/stream", "", ""))


class LatestFrameStore:
    """Thread-safe single-slot store; old frames are overwritten, never queued."""
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._sequence = 0
        self._captured_at = 0.0

    def publish(self, frame: bytes) -> int:
        with self._lock:
            self._frame = frame
            self._sequence += 1
            self._captured_at = time.monotonic()
            return self._sequence

    def get_latest(self):
        with self._lock:
            return self._frame, self._sequence, self._captured_at


def iter_mjpeg_frames(url: str, stop_event: threading.Event):
    """Yield complete JPEG frames from an MJPEG response."""
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

                frame = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                yield frame


def _put_latest(frames: queue.Queue, frame: bytes) -> None:
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


def camera_reader(frames: LatestFrameStore, stop_event: threading.Event) -> None:
    """Continuously read MJPEG and retain only the newest complete frame."""
    image_path = os.getenv("EDITH_IMAGE_PATH", "").strip()
    if image_path:
        path = Path(image_path).expanduser()
        print(f"[EDITH GEMINI] Using local image source: {path}")
        while not stop_event.is_set():
            try:
                with Image.open(path) as source:
                    image = source.convert("RGB")
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=90)
                    frames.publish(buffer.getvalue())
            except Exception as exc:
                print(f"[EDITH GEMINI] Local image error: {exc}")
                stop_event.wait(2.0)
                continue
            stop_event.wait(FRAME_INTERVAL_SECONDS)
        return

    try:
        stream_url = get_camera_stream_url()
    except Exception as exc:
        print(f"[EDITH GEMINI] Camera source unavailable: {exc}")
        stop_event.set()
        return

    print(f"[EDITH GEMINI] Connecting to continuous camera stream: {stream_url}")
    successful_frames = 0
    failed_streams = 0
    last_frame_time = None
    while not stop_event.is_set():
        try:
            for frame in iter_mjpeg_frames(stream_url, stop_event):
                if stop_event.is_set():
                    break
                sequence = frames.publish(frame)
                successful_frames += 1
                now = time.monotonic()
                frame_gap = now - last_frame_time if last_frame_time is not None else 0.0
                last_frame_time = now
                if successful_frames <= 3 or successful_frames % 150 == 0:
                    print(
                        "[EDITH CAMERA STREAM] "
                        f"frames={successful_frames} latest={sequence} "
                        f"size={len(frame) / 1024:.1f} KB "
                        f"gap={frame_gap:.3f} s reconnects={failed_streams}"
                    )
            if stop_event.is_set():
                break
            raise RuntimeError("Camera MJPEG stream ended unexpectedly.")
        except Exception as exc:
            failed_streams += 1
            if not stop_event.is_set():
                print(f"[EDITH GEMINI] Camera stream interrupted (reconnect {failed_streams}): {exc}")
                stop_event.wait(1.0)
    print("[EDITH GEMINI] Camera stream reader stopped.")


class LocalCameraProxy(ThreadingHTTPServer):
    """Serve EDITH's latest camera frames to local browser clients."""
    daemon_threads = True
    allow_reuse_address = True


def start_camera_proxy(frames: LatestFrameStore, stop_event: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/stream"):
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=edithframe",
            )
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            last_sequence = -1
            try:
                while not stop_event.is_set():
                    frame, sequence, _ = frames.get_latest()
                    if frame is None or sequence == last_sequence:
                        time.sleep(0.05)
                        continue

                    self.wfile.write(
                        b"--edithframe\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                        + frame
                        + b"\r\n"
                    )
                    self.wfile.flush()
                    last_sequence = sequence
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, format, *args):
            return

    server = LocalCameraProxy(("127.0.0.1", 8765), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="edith-local-camera-proxy",
        daemon=True,
    )
    thread.start()
    print("[EDITH CAMERA] Local browser preview: http://127.0.0.1:8765/stream")
    return server


class GeminiLiveAssistant:
    """Synchronous interface to one persistent asynchronous Gemini Live session."""

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = MODEL
        self._requests: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: Exception | None = None
        self._active_request = None
        self._ask_lock = threading.Lock()
        self._client = None

    def start(self, timeout: float = 50.0) -> None:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is missing from .env.")
        if self._thread and self._thread.is_alive():
            if self._startup_error:
                raise RuntimeError(str(self._startup_error))
            return

        self._thread = threading.Thread(
            target=self._thread_main,
            name="edith-gemini-live",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(timeout):
            self.close()
            raise TimeoutError(
                "Gemini Live did not become ready. Check the camera source, "
                "network connection, and API access."
            )
        if self._startup_error:
            raise RuntimeError(f"Gemini Live startup failed: {self._startup_error}")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_session())
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            if not self._stop.is_set():
                print(f"[EDITH GEMINI] Session error: {exc}")
                traceback.print_exc()

    async def _send_frames(
        self, session, frames: LatestFrameStore,
        camera_ready: threading.Event, send_lock: asyncio.Lock,
    ) -> None:
        sent_sequence = 0
        loop = asyncio.get_running_loop()
        next_send = loop.time()
        while not self._stop.is_set():
            delay = next_send - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            frame, sequence, captured_at = frames.get_latest()
            if frame is None or sequence == sent_sequence:
                await asyncio.sleep(0.05)
                continue
            async with send_lock:
                # Re-read after acquiring the lock to avoid sending a frame
                # that became stale while waiting behind a question.
                frame, sequence, captured_at = frames.get_latest()
                if frame is None or sequence == sent_sequence:
                    continue
                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[types.Part(inline_data=types.Blob(
                            data=frame, mime_type="image/jpeg"
                        ))],
                    ),
                    turn_complete=False,
                )
            sent_sequence = sequence
            next_send = loop.time() + FRAME_INTERVAL_SECONDS
            if sent_sequence <= 3 or sent_sequence % 30 == 0:
                age = max(0.0, time.monotonic() - captured_at)
                print(f"[EDITH GEMINI] Camera frames sent: {sent_sequence} (frame age {age:.2f}s)")
            if sent_sequence >= 3 and not camera_ready.is_set():
                camera_ready.set()
                print("[EDITH GEMINI] Live camera feed confirmed.")
            await asyncio.sleep(0)

    async def _send_questions(
        self, session, frames: LatestFrameStore, send_lock: asyncio.Lock
    ) -> None:
        while not self._stop.is_set():
            # Do not associate a late response with a newer request if the
            # caller timed out while Gemini was still generating its answer.
            while self._active_request is not None and not self._stop.is_set():
                await asyncio.sleep(0.05)

            try:
                request = await asyncio.to_thread(
                    self._requests.get, True, 0.5
                )
            except queue.Empty:
                continue

            if request is None:
                return

            self._active_request = request
            try:
                # Serialize the read and send with background frame delivery.
                # This makes the image as fresh as possible at send time.
                async with send_lock:
                    frame, frame_sequence, captured_at = frames.get_latest()
                    parts = []
                    if frame is not None:
                        parts.append(types.Part(inline_data=types.Blob(
                            data=frame, mime_type="image/jpeg"
                        )))
                    audio_data = request.get("audio_data")
                    if audio_data:
                        parts.append(types.Part(inline_data=types.Blob(
                            data=audio_data, mime_type="audio/pcm;rate=16000"
                        )))
                    else:
                        parts.append(types.Part(text=request["question"]))

                    await session.send_client_content(
                        turns=types.Content(role="user", parts=parts),
                        turn_complete=True,
                    )
                if frame is not None:
                    age = max(0.0, time.monotonic() - captured_at)
                    print(
                        "[EDITH GEMINI] Question paired with latest camera "
                        f"frame #{frame_sequence} (age {age:.2f}s)."
                    )
            except Exception as exc:
                request["error"] = exc
                request["done"].set()
                self._active_request = None
                raise

    @staticmethod
    def _finish_request(request) -> None:
        """Publish a completed turn exactly once."""
        if request["done"].is_set():
            return
        request["result"] = {
            "input_text": "".join(request["input_text_parts"]).strip(),
            "text": "".join(request["text_parts"]).strip(),
            "audio": b"".join(request["audio_parts"]),
        }
        request["done"].set()

    async def _finish_after_transcription_grace(self, request) -> None:
        """Allow late input-transcription events to arrive after turn_complete."""
        await asyncio.sleep(1.5)
        if self._active_request is request:
            self._finish_request(request)
            self._active_request = None

    async def _receive_responses(self, session) -> None:
        """Receive Live events; treat socket closure during shutdown as normal."""
        while not self._stop.is_set():
            try:
                async for response in session.receive():
                    content = response.server_content
                    if content is None:
                        continue

                    request = self._active_request
                    if request is not None:
                        input_transcript = getattr(content, "input_transcription", None)
                        if input_transcript and input_transcript.text:
                            request["input_text_parts"].append(input_transcript.text)
                        if input_transcript and getattr(input_transcript, "finished", False):
                            request["input_transcription_finished"] = True

                        # Input transcription and model-turn completion can arrive
                        # independently, so collect both before releasing the turn.
                        interim = getattr(content, "interim_input_transcription", None)
                        if interim and interim.text and not request["input_text_parts"]:
                            request["interim_input_text"] = interim.text

                        transcript = getattr(content, "output_transcription", None)
                        if transcript and transcript.text:
                            request["text_parts"].append(transcript.text)

                        model_turn = getattr(content, "model_turn", None)
                        if model_turn:
                            for part in model_turn.parts:
                                inline_data = getattr(part, "inline_data", None)
                                if inline_data and inline_data.data:
                                    request["audio_parts"].append(inline_data.data)

                        if getattr(content, "turn_complete", False):
                            request["turn_complete"] = True
                            if request.get("input_transcription_finished"):
                                self._finish_request(request)
                                self._active_request = None
                            elif not request.get("transcription_grace_started"):
                                request["transcription_grace_started"] = True
                                asyncio.create_task(
                                    self._finish_after_transcription_grace(request)
                                )

                        elif (
                            request.get("turn_complete")
                            and request.get("input_transcription_finished")
                        ):
                            self._finish_request(request)
                            self._active_request = None

                    if self._stop.is_set():
                        return

            except Exception as exc:
                # Closing the Live WebSocket can surface as APIError 1000 or
                # ConnectionClosedError. If shutdown was already requested,
                # this is expected and must not become an unhandled task error.
                if self._stop.is_set():
                    print("[EDITH GEMINI] Response receiver stopped cleanly.")
                    return
                raise

            if not self._stop.is_set():
                await asyncio.sleep(0.05)

    async def _run_session(self) -> None:
        frames = LatestFrameStore()
        camera_ready = threading.Event()
        send_lock = asyncio.Lock()
        reader = threading.Thread(
            target=camera_reader,
            args=(frames, self._stop),
            name="edith-gemini-camera",
            daemon=True,
        )
        reader.start()
        camera_proxy = start_camera_proxy(frames, self._stop)

        try:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(api_version="v1beta"),
            )
            config = types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                system_instruction=types.Content(
                    parts=[types.Part(text=SYSTEM_INSTRUCTION)]
                ),
            )

            print(f"[EDITH GEMINI] Connecting to {self.model}...")
            async with self._client.aio.live.connect(
                model=self.model,
                config=config,
            ) as session:
                print("[EDITH GEMINI] Persistent Live session connected.")
                tasks = [
                    asyncio.create_task(self._send_frames(session, frames, camera_ready, send_lock)),
                    asyncio.create_task(self._send_questions(session, frames, send_lock)),
                    asyncio.create_task(self._receive_responses(session)),
                ]

                camera_ok = await asyncio.to_thread(camera_ready.wait, 35.0)
                if not camera_ok:
                    raise TimeoutError(
                        "Gemini connected, but no camera frames reached the session."
                    )
                self._ready.set()

                stop_waiter = asyncio.create_task(asyncio.to_thread(self._stop.wait))
                done, pending = await asyncio.wait(
                    tasks + [stop_waiter],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    if task is stop_waiter or task.cancelled():
                        continue
                    error = task.exception()
                    if error:
                        raise error
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._stop.set()
            self._ready.set()
            camera_proxy.shutdown()
            camera_proxy.server_close()
            reader.join(timeout=3.0)
            if self._client is not None:
                await self._client.aio.aclose()
                self._client = None

    def ask(
        self,
        question: str,
        audio_data: bytes | None = None,
        timeout: float = 90.0,
    ) -> dict | None:
        """Send typed text or a 16 kHz PCM utterance; return transcript and audio."""
        question = str(question).strip()
        if not question and not audio_data:
            return None
        if self._stop.is_set() or not self._ready.is_set():
            return None
        if self._startup_error:
            raise RuntimeError(str(self._startup_error))

        with self._ask_lock:
            request = {
                "question": question,
                "audio_data": audio_data,
                "done": threading.Event(),
                "input_text_parts": [],
                "text_parts": [],
                "audio_parts": [],
                "result": None,
                "error": None,
            }
            self._requests.put(request)
            if not request["done"].wait(timeout):
                print(
                    "[EDITH GEMINI] Response timed out. "
                    "Stopping this session so the next request can reconnect cleanly."
                )
                # The old turn may never emit turn_complete. Stop this session
                # rather than leaving _active_request blocking the request queue.
                self._stop.set()
                return None
            if request["error"]:
                raise RuntimeError(str(request["error"]))
            return request["result"]

    def close(self) -> None:
        self._stop.set()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        print("[EDITH GEMINI] Live session closed.")


def main() -> None:
    """Standalone diagnostic mode retained for direct module testing."""
    assistant = GeminiLiveAssistant()
    try:
        assistant.start()
        print("\nEDITH GEMINI LIVE")
        print("The camera feed is active. Ask a question or type exit.\n")
        while True:
            question = input("[YOU] ").strip()
            if question.lower() in {"exit", "quit", "stop"}:
                break
            if not question:
                continue
            result = assistant.ask(question)
            if result and result.get("text"):
                print(f"\n[EDITH / GEMINI] {result['text']}\n")
    except KeyboardInterrupt:
        print("\n[EDITH GEMINI] Shutdown requested.")
    except Exception as exc:
        print(f"[EDITH GEMINI ERROR] {exc}")
        traceback.print_exc()
    finally:
        assistant.close()


if __name__ == "__main__":
    main()
