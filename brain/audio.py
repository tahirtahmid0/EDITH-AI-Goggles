import subprocess
import socket
import struct
import wave
import queue
import threading
import os
import time
import torch
import sys
import random
import traceback
import tempfile
import atexit

import re
from visual_event_memory import load_events, search_events, get_recent_events
from datetime import datetime, date
from pathlib import Path
from collections import deque
from silero_vad import load_silero_vad

# ==========================================
# EDITH - WIRELESS SPEAKER OUTPUT
# ==========================================

TTS_ESP32_IP = "10.0.100.41"
TTS_ESP32_PORT = 5006

TTS_SAMPLE_RATE = 16000
TTS_CHANNELS = 1
TTS_SAMPLE_WIDTH = 2  # 16-bit PCM

TTS_PACKET_BYTES = 640  # 20 ms of audio

FFMPEG_PATH = "/opt/homebrew/bin/ffmpeg"

# ==========================================
# EDITH - SILERO AUTOMATIC LISTENING
# ESP32 -> UDP -> SILERO VAD -> WHISPER
# ==========================================


HOST = "0.0.0.0"
PORT = 5005

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2

# ESP32 sends 256 samples per UDP packet.
PACKET_SAMPLES = 256
PACKET_BYTES = PACKET_SAMPLES * SAMPLE_WIDTH

# Silero requires 512 samples at 16 kHz.
CHUNK_SAMPLES = 512
CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_WIDTH

# Silero detection settings.
START_THRESHOLD = 0.45
STOP_THRESHOLD = 0.30

START_CHUNKS = 2
STOP_CHUNKS = 25

# Keep approximately 256 ms before speech detection.
PRE_ROLL_CHUNKS = 8

# Idle conversation settings
IDLE_TIMEOUT = 60  # Seconds before EDITH checks in

MODEL = "mlx-community/whisper-base-mlx"

BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_DIR = BASE_DIR / "temp"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from brain import brain as vision

capture_image = vision.capture_image
detect_objects = vision.detect_objects
display_results = vision.display_results
MoondreamSession = vision.MoondreamSession
understand_scene = vision.understand_scene

audio_queue = queue.Queue(maxsize=3)

# EDITH audio state
speaking = threading.Event()

# Stores a memory temporarily until the user confirms it.
pending_explicit_memory = None

# Track the last time the user was active
last_activity_time = time.monotonic()

os.environ["PATH"] = (
    "/opt/homebrew/bin:"
    + os.environ.get("PATH", "")
)

print("[EDITH] FFmpeg location: /opt/homebrew/bin/ffmpeg")

torch.set_num_threads(1)

# ==========================================
# EDITH - TEXT TO SPEECH
# ==========================================



def speak(text):

    if not text:
        return

    print(f"[EDITH] Speaking: {text}")

    speaking.set()

    try:

        output_mode = os.getenv("EDITH_TTS_OUTPUT", "esp32").strip().lower()

        if output_mode in ("console", "silent", "none", "off"):
            return

        if output_mode == "local":
            subprocess.run(
                ["/usr/bin/say", text],
                check=True,
                timeout=60
            )
            return

        # ======================================
        # GENERATE SPEECH USING MACOS TTS
        # ======================================

        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix="edith_tts_",
            dir=TEMP_DIR
        ) as temporary_directory:

            audio_path = Path(temporary_directory) / "speech.aiff"

            print("[EDITH TTS] Generating speech...")

            subprocess.run(
                [
                    "/usr/bin/say",
                    "-o",
                    str(audio_path),
                    text
                ],
                check=True,
                timeout=30
            )

            # ======================================
            # CONVERT SPEECH TO ESP32 PCM FORMAT
            # ======================================

            print("[EDITH TTS] Converting audio to PCM...")

            conversion = subprocess.run(
                [
                    FFMPEG_PATH,
                    "-v", "error",
                    "-i", str(audio_path),
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ac", str(TTS_CHANNELS),
                    "-ar", str(TTS_SAMPLE_RATE),
                    "pipe:1"
                ],
                capture_output=True,
                check=True,
                timeout=30
            )

            pcm_audio = conversion.stdout

            if not pcm_audio:
                raise RuntimeError("FFmpeg produced empty audio.")

            # Ensure the PCM data contains complete 16-bit samples.
            pcm_audio = pcm_audio[:len(pcm_audio) - (len(pcm_audio) % 2)]

            print(
                f"[EDITH TTS] PCM generated: "
                f"{len(pcm_audio)} bytes"
            )

            # ======================================
            # TRANSMIT AUDIO TO ESP32
            # ======================================

            print("[EDITH TTS] Streaming audio to ESP32...")

            with socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM
            ) as speaker_socket:

                packets_sent = 0

                for offset in range(
                    0,
                    len(pcm_audio),
                    TTS_PACKET_BYTES
                ):

                    packet = pcm_audio[
                        offset:offset + TTS_PACKET_BYTES
                    ]

                    if not packet:
                        continue

                    speaker_socket.sendto(
                        packet,
                        (TTS_ESP32_IP, TTS_ESP32_PORT)
                    )

                    packets_sent += 1

                    # Pace transmission to match 16 kHz playback.
                    packet_duration = (
                        len(packet)
                        / (
                            TTS_SAMPLE_RATE
                            * TTS_CHANNELS
                            * TTS_SAMPLE_WIDTH
                        )
                    )

                    time.sleep(packet_duration)

            print(
                f"[EDITH TTS] Transmission complete. "
                f"Packets sent: {packets_sent}"
            )

    except subprocess.TimeoutExpired:
        print("[EDITH ERROR] Speech synthesis or conversion timed out.")

    except subprocess.CalledProcessError as error:
        print("[EDITH ERROR] Audio generation failed:", error)

    except Exception as error:
        print("[EDITH ERROR] Speaker output failed:", error)
        traceback.print_exc()

    finally:

        print("[EDITH] Speech finished. Waiting for microphone cooldown...")

        time.sleep(1)

        speaking.clear()

        print("[EDITH] Cooldown complete. Resuming listening.")



def respond(text):
    """
    Save EDITH's actual response to conversational memory,
    then speak it aloud.
    """

    if not text:
        return

    try:
        vision.memory.save_conversation(
            role="assistant",
            content=text,
            visual_memory_id=getattr(vision, "CURRENT_MEMORY_ID", None)
        )

    except Exception as error:
        print("[EDITH MEMORY] Could not save assistant response:", error)

    speak(text)


# ==========================================
# MANAGED MOONDREAM SESSION
# ==========================================

_MOONDREAM_LOCK = threading.RLock()
_MOONDREAM_SESSION = None
_MOONDREAM_UNLOAD_TIMER = None

try:
    MOONDREAM_IDLE_SECONDS = max(
        15,
        int(os.getenv("EDITH_MOONDREAM_IDLE_SECONDS", "120"))
    )
except ValueError:
    MOONDREAM_IDLE_SECONDS = 120


def close_moondream_session():
    """Release Moondream safely when idle or before loading YOLO."""
    global _MOONDREAM_SESSION, _MOONDREAM_UNLOAD_TIMER

    with _MOONDREAM_LOCK:
        if _MOONDREAM_UNLOAD_TIMER is not None:
            _MOONDREAM_UNLOAD_TIMER.cancel()
            _MOONDREAM_UNLOAD_TIMER = None

        session = _MOONDREAM_SESSION
        _MOONDREAM_SESSION = None

        if session is not None:
            print("[EDITH] Releasing cached Moondream session...")
            session.close()


def _get_moondream_session():
    global _MOONDREAM_SESSION

    if _MOONDREAM_SESSION is None:
        print("[EDITH] Creating a lazy Moondream session...")
        _MOONDREAM_SESSION = MoondreamSession()

    return _MOONDREAM_SESSION


def _schedule_moondream_unload():
    global _MOONDREAM_UNLOAD_TIMER

    if _MOONDREAM_UNLOAD_TIMER is not None:
        _MOONDREAM_UNLOAD_TIMER.cancel()

    _MOONDREAM_UNLOAD_TIMER = threading.Timer(
        MOONDREAM_IDLE_SECONDS,
        close_moondream_session
    )
    _MOONDREAM_UNLOAD_TIMER.daemon = True
    _MOONDREAM_UNLOAD_TIMER.start()


def understand_scene_with_updates(image, question):
    """
    Ask Moondream using one managed session.

    The model stays available for follow-up questions, then unloads after
    an idle timeout or immediately before YOLO is run to protect Mac RAM.
    """
    completed = threading.Event()
    result_data = {}
    error_data = {}

    progress_messages = [
        "I'm still examining the image.",
        "I'm checking the visual details carefully.",
        "Almost there. I'm putting everything together.",
        "I'm taking a closer look at the scene."
    ]

    def moondream_worker():
        try:
            print("[EDITH] Starting Moondream analysis...")

            with _MOONDREAM_LOCK:
                session = _get_moondream_session()
                result_data["result"] = understand_scene(
                    image,
                    question=question,
                    session=session
                )
                _schedule_moondream_unload()

        except Exception as error:
            error_data["error"] = error
            error_data["traceback"] = traceback.format_exc()
            close_moondream_session()

        finally:
            completed.set()

    worker = threading.Thread(
        target=moondream_worker,
        name="EDITH-Moondream",
        daemon=True
    )
    worker.start()

    message_index = 0

    while not completed.wait(timeout=15):
        message = progress_messages[message_index % len(progress_messages)]
        message_index += 1
        print("[EDITH] Providing a progress update.")
        speak(message)

    worker.join()

    if "error" in error_data:
        print("[EDITH ERROR] Moondream analysis failed:")
        print(error_data["traceback"])
        raise RuntimeError(
            "Moondream could not complete the visual analysis."
        ) from error_data["error"]

    return result_data["result"]


def run_object_detection(image):
    """Run YOLO only after releasing Moondream to limit memory pressure."""
    with _MOONDREAM_LOCK:
        close_moondream_session()
        return vision.detect_objects(image)


atexit.register(close_moondream_session)


# ==========================================
# VISUAL QUESTION ROUTING
# ==========================================

_SCENE_DESCRIPTION_PHRASES = (
    "describe the object in front of me",
    "describe what's in front of me",
    "describe what is in front of me",
    "describe what you can see",
    "describe my surroundings",
    "describe the scene",
    "describe this scene",
    "what's in front of me",
    "what is in front of me",
    "tell me what's in front of me",
    "tell me what is in front of me",
    "tell me what i'm looking at",
    "what am i looking at",
)

_VISUAL_QUESTION_MARKERS = (
    "what colour", "what color", "colour of", "color of",
    "wearing", "holding", "how many", "is there", "are there",
    "what is this", "what's this", "what does",
    "what is on", "what's on", "what is in", "what's in",
    "what is behind", "what's behind", "behind the",
    "next to", "beside", "near the", "where is the", "where is a",
    "where is my", "can you see", "identify", "describe",
    "what is the person", "what's the person",
    "what is the object", "what's the object",
)


def is_visual_question(command):
    """Return True for current-image questions, not historical memory queries."""
    normalised = command.lower().strip()

    if not normalised:
        return False

    return (
        any(phrase in normalised for phrase in _SCENE_DESCRIPTION_PHRASES)
        or any(marker in normalised for marker in _VISUAL_QUESTION_MARKERS)
    )


def build_visual_question_prompt(command):
    """Build a grounded Moondream prompt for either a scene or specific query."""
    normalised = command.lower().strip()

    if any(phrase in normalised for phrase in _SCENE_DESCRIPTION_PHRASES):
        return (
            "Describe the scene in one or two concise sentences. "
            "Identify only objects and details clearly visible in the image. "
            "Do not give advice or recommendations. Do not invent details. "
            "If something is unclear, say so."
        )

    return (
        f"The user's question is: {command}\n"
        "Answer that question directly using only details clearly visible "
        "in this image. Do not guess, infer hidden details, or invent objects. "
        "If the image does not provide enough evidence, say that you cannot "
        "tell from this image. Keep the answer concise and conversational."
    )


def answer_live_visual_question(command):
    """Capture the configured image source and answer with Moondream."""
    speak("Give me a moment. I'm checking the image.")
    print("[EDITH] Answering a live visual question:", command)

    try:
        image = capture_image()
        result = understand_scene_with_updates(
            image,
            question=build_visual_question_prompt(command)
        )
        answer = result.get("scene_description", "").strip()

        if not answer:
            answer = "I couldn't determine that from the image."

        print("[EDITH MOONDREAM] Answer:", answer)
        respond(answer)

    except Exception as error:
        print("[EDITH ERROR] Visual question failed:", error)
        respond(
            "I couldn't analyse the image. Please check the image source "
            "and try again."
        )



def get_object_position(detection):

    x1, y1, x2, y2 = detection["bbox"]

    # Calculate the centre of the bounding box.
    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2

    # Get camera dimensions from brain.py.
    width = vision.CAMERA_WIDTH
    height = vision.CAMERA_HEIGHT

    # Determine horizontal position.
    if center_x < width / 3:
        horizontal = "left"

    elif center_x > (width * 2 / 3):
        horizontal = "right"

    else:
        horizontal = "centre"

    # Determine vertical position.
    if center_y < height / 3:
        vertical = "top"

    elif center_y > (height * 2 / 3):
        vertical = "bottom"

    else:
        vertical = "middle"

    # Convert coordinates into natural language.
    if vertical == "middle" and horizontal == "centre":
        return "in the centre of my view"

    elif vertical == "middle":
        return f"on the {horizontal} side of my view"

    elif horizontal == "centre":
        return f"near the {vertical} centre of my view"

    else:
        return f"in the {vertical}-{horizontal} of my view"


def format_memory_date(timestamp, exact=False):
    """
    Convert a stored memory timestamp into natural language.
    """

    try:
        memory_datetime = datetime.fromisoformat(timestamp)
        memory_date = memory_datetime.date()
        today = datetime.now().astimezone().date()

        days_ago = (today - memory_date).days

        # User explicitly wants the exact date.
        if exact:
            suffix = (
                "th" if 10 <= memory_date.day % 100 <= 13
                else {1: "st", 2: "nd", 3: "rd"}.get(
                    memory_date.day % 10, "th"
                )
            )

            return (
                memory_date.strftime("%B ")
                + str(memory_date.day)
                + suffix
                + memory_date.strftime(", %Y")
            )

        if days_ago == 0:
            return "earlier today"

        elif days_ago == 1:
            return "yesterday"

        elif 1 < days_ago <= 6:
            return memory_date.strftime("on %A")

        elif days_ago < 0:
            return (
                "on "
                + memory_date.strftime("%B ")
                + str(memory_date.day)
            )

        else:
            return (
                "on "
                + memory_date.strftime("%B ")
                + str(memory_date.day)
                + memory_date.strftime(", %Y")
            )

    except Exception as error:
        print("[EDITH MEMORY] Date formatting error:", error)
        return "at an earlier time"



def get_visual_event_response(command):
    """
    Retrieve and format historical visual tracking events.

    Returns None when the command is not a tracking-history question.
    """

    command = command.lower().strip()

    event_keywords = [
        "move", "moved", "moving", "movement",
        "appear", "appeared", "appearance",
        "disappear", "disappeared", "disappearance",
        "vanish", "vanished",
        "what happened recently",
        "what happened to",
        "has anything changed",
        "did anything change",
        "tracking history",
        "visual history"
    ]

    if not any(keyword in command for keyword in event_keywords):
        return None

    # Identify the requested event type.
    if any(word in command for word in [
        "disappear", "disappeared", "disappearance",
        "vanish", "vanished"
    ]):
        event_type = "object_disappeared"

    elif any(word in command for word in [
        "appear", "appeared", "appearance"
    ]):
        event_type = "object_appeared"

    elif any(word in command for word in [
        "move", "moved", "moving", "movement"
    ]):
        event_type = "object_moved"

    else:
        event_type = None

    # Identify an object class mentioned by the user.
    all_events = load_events()

    known_classes = {
        event.get("class_name")
        for event in all_events
        if event.get("class_name")
    }

    selected_class = None

    for class_name in sorted(known_classes, key=len, reverse=True):

        readable_name = class_name.replace("_", " ")

        if re.search(
            r"\b" + re.escape(readable_name) + r"\b",
            command
        ):
            selected_class = class_name
            break

    # Retrieve matching records.
    if selected_class or event_type:

        events = search_events(
            class_name=selected_class,
            event_type=event_type,
            limit=20
        )

    else:
        events = get_recent_events(limit=20)

    if not events:

        if selected_class:
            return (
                f"I couldn't find any recent tracking events "
                f"for the {selected_class.replace('_', ' ')}."
            )

        return "I couldn't find any matching events in my visual tracking history."

    # Convert timestamps to local time.
    def parse_event_time(timestamp):

        try:
            return datetime.fromisoformat(timestamp).astimezone()
        except (TypeError, ValueError):
            return None

    # Sort oldest first for a natural chronological explanation.
    events = sorted(
        events,
        key=lambda event: parse_event_time(event.get("timestamp"))
        or datetime.min.astimezone()
    )

    # Remove exact duplicate records.
    unique_events = []
    seen_events = set()

    for event in events:

        event_key = (
            event.get("timestamp"),
            event.get("event"),
            event.get("class_name"),
            event.get("visual_identity_id"),
            event.get("displacement_px")
        )

        if event_key not in seen_events:
            seen_events.add(event_key)
            unique_events.append(event)

    # Select the three most recent events, then present them chronologically.
    local_timezone = datetime.now().astimezone().tzinfo

    unique_events.sort(
        key=lambda event: (
                parse_event_time(event.get("timestamp"))
                or datetime.min.replace(tzinfo=local_timezone)
        ),
        reverse=True
    )

    unique_events = list(reversed(unique_events[:3]))

    # Describe the selected events.
    descriptions = []

    for event in unique_events:

        object_name = event.get(
            "class_name", "object"
        ).replace("_", " ")

        event_name = event.get("event")

        event_datetime = parse_event_time(
            event.get("timestamp")
        )

        if event_datetime:
            timestamp = (
                event_datetime.strftime("%B ")
                + str(event_datetime.day)
                + " at "
                + event_datetime.strftime("%I:%M %p").lstrip("0")
            )
        else:
            timestamp = "an earlier time"

        if event_name == "object_moved":

            displacement = event.get("displacement_px")

            if isinstance(displacement, (int, float)):
                description = (
                    f"The {object_name} moved approximately "
                    f"{displacement:.0f} pixels"
                )
            else:
                description = f"The {object_name} was detected moving"

        elif event_name == "object_appeared":
            description = (
                f"A {object_name} appeared in my camera view"
            )

        elif event_name == "object_disappeared":
            description = (
                f"My tracker stopped detecting the {object_name}"
            )

        else:
            continue

        descriptions.append(
            f"{description} at {timestamp}"
        )

        if len(descriptions) >= 3:
            break

    if not descriptions:
        return (
            "I found tracking records, but couldn't interpret "
            "their event details."
        )

    if len(descriptions) == 1:
        return (
            "According to my visual tracking history, "
            + descriptions[0]
            + "."
        )

    response = (
        "According to my visual tracking history, "
        + "; then, ".join(descriptions)
        + "."
    )

    return response




def process_command(text):
    """
    Interpret recognised speech and execute the appropriate EDITH command.
    """

    global pending_explicit_memory

    command = text.lower().strip()

    if not command:
        return

    print(f"[EDITH] Processing command: {command}")

    # ==========================================
    # COMMAND: CONFIRM PENDING EXPLICIT MEMORY
    # ==========================================

    if pending_explicit_memory is not None:

        confirmation = command.strip(" .,!?")

        yes_phrases = [
            "yes",
            "yeah",
            "yep",
            "correct",
            "that's right",
            "thats right",
            "sure",
            "absolutely",
            "save it",
            "remember it"
        ]

        no_phrases = [
            "no",
            "nope",
            "nah",
            "cancel",
            "don't save it",
            "dont save it",
            "forget it"
        ]

        if any(
            confirmation == phrase
            or confirmation.startswith(phrase + " ")
            for phrase in yes_phrases
        ):

            memory_content = pending_explicit_memory

            # Clear the pending memory before saving.
            pending_explicit_memory = None

            memory_id = vision.memory.save_explicit_memory(
                memory_content
            )

            if memory_id:
                response = (
                    "Of course. I'll remember that "
                    + memory_content
                )
            else:
                response = (
                    "I couldn't save that memory. "
                    "Could you tell me again?"
                )

            print("[EDITH MEMORY] Response:", response)
            respond(response)

            return

        elif any(
            confirmation == phrase
            or confirmation.startswith(phrase + " ")
            for phrase in no_phrases
        ):

            pending_explicit_memory = None

            response = "No problem. I won't save that."

            print("[EDITH MEMORY] Response:", response)
            respond(response)

            return

        else:

            response = (
                "I haven't saved it yet. "
                "Please say yes to save it, or no to cancel."
            )

            respond(response)

            return

    # ==========================================
    # COMMAND: SAVE EXPLICIT MEMORY
    # ==========================================

    memory_command = command.strip()

    if memory_command.startswith("edith"):
        memory_command = memory_command[5:].lstrip(" ,.!?")

    remember_phrases = [
        "please remember that",
        "please remember this",
        "remember that",
        "remember this",
        "don't forget that",
        "dont forget that"
    ]

    for phrase in remember_phrases:

        if memory_command.startswith(phrase):

            memory_content = memory_command[len(phrase):].strip()
            memory_content = memory_content.strip(" .,!?")

            if memory_content:

                # Hold the memory temporarily.
                # Do not save it until the user confirms.
                pending_explicit_memory = memory_content

                response = (
                    "Just to confirm, should I remember that "
                    + memory_content
                    + "?"
                )

            else:
                response = "What would you like me to remember?"

            print("[EDITH MEMORY] Response:", response)
            respond(response)

            return

    # ==========================================
    # COMMAND: EXPLICIT MEMORY RETRIEVAL
    # ==========================================

    explicit_memory_phrases = [
        "where did i put",
        "where did i leave",
        "where did i keep",
        "where do i keep",
        "where do i put",
        "where are my",
        "where is my",
        "where can i find",
        "where have i put",
        "what did i tell you to remember",
        "what did i ask you to remember",
        "what do you remember about",
        "do you remember that i",
        "when did i tell you",
        "when did i ask you",
        "when exactly did i tell you"
    ]

    if any(phrase in command for phrase in explicit_memory_phrases):

        print("[EDITH MEMORY] Searching explicit memories...")

        memories = vision.memory.search_explicit_memories(
            command,
            limit=5
        )

        print("\n[EDITH MEMORY] Search results:")

        for item in memories:
            print(
                f"ID: {item['id']} | "
                f"Relevance: {item['relevance']} | "
                f"Date: {item['timestamp']} | "
                f"Memory: {item['content']}"
            )

        if memories:

            best_memory = memories[0]

            content = best_memory["content"]
            timestamp = best_memory["timestamp"]

            exact_date = any(phrase in command for phrase in [
                "when did i tell you",
                "when did i ask you",
                "when exactly did i tell you"
            ])

            date_text = format_memory_date(
                timestamp,
                exact=exact_date
            )

            if exact_date:
                response = f"You told me on {date_text}: {content}"
            else:
                response = f"You told me {date_text} that {content}"


        else:

            visual = vision.memory.search_visual_memories(command, limit=1)

            if visual and visual[0].get("scene_description"):

                when = format_memory_date(visual[0]["captured_at"])

                response = (

                    f"{when.capitalize()}, I saw: "

                    f"{visual[0]['scene_description']}"

                )

            else:

                response = "I couldn't find that in my memories."

        print("[EDITH MEMORY] Response:", response)

        respond(response)

        return

    # ==========================================
    # COMMAND: VISUAL MEMORY RETRIEVAL
    # ==========================================

    if any(phrase in command for phrase in [
        "do you remember",
        "did you see earlier",
        "what did you see earlier",
        "remember the",
        "what was there before",
        "what did you see before",
        "when did you see",
        "when was that",
        "what date",
        "which date",
        "when exactly",
        "exactly when"
    ]):

        print("[EDITH MEMORY] Searching visual memories...")

        # General questions should retrieve recent visual memories.
        general_recall_phrases = [
            "what did you see earlier",
            "what did you see before",
            "what was there before",
            "what have you seen",
            "do you remember what you saw",
            "describe what you saw earlier"
        ]

        if any(phrase in command for phrase in general_recall_phrases):

            print("[EDITH MEMORY] Retrieving recent visual memories...")

            recent_records = vision.memory.get_recent_memories(limit=5)

            memories = []

            for record in recent_records:

                memory_data = vision.memory.get_visual_memory(record[0])

                if memory_data:
                    memories.append(memory_data)

        else:

            # Specific questions should use keyword search.
            memories = vision.memory.search_visual_memories(
                command,
                limit=5
            )

        if memories:

            # Keep only memories that contain useful scene descriptions.
            valid_memories = []

            for memory in memories:

                description = memory.get("scene_description")

                if description and description.strip():
                    valid_memories.append(memory)

            # Remove exact duplicate descriptions.
            unique_descriptions = []
            seen_descriptions = set()

            for memory in valid_memories:

                description = memory["scene_description"].strip()
                normalised = description.lower().strip(" .!?")

                if normalised not in seen_descriptions:
                    seen_descriptions.add(normalised)
                    unique_descriptions.append(description)

            exact_date = any(phrase in command for phrase in [
                "when did you see",
                "when was that",
                "what date",
                "which date",
                "when exactly",
                "exactly when"
            ])

            if valid_memories:

                best_memory = valid_memories[0]

                date_text = format_memory_date(
                    best_memory["captured_at"],
                    exact=exact_date
                )

                if exact_date:

                    response = (
                        f"I saw that on {date_text}. "
                        f"{unique_descriptions[0]}"
                    )

                elif len(unique_descriptions) == 1:

                    response = (
                        f"{date_text.capitalize()}, "
                        f"{unique_descriptions[0]}"
                    )

                else:

                    # Mention the date once and combine the remembered scenes.
                    response = (
                        f"{date_text.capitalize()}, "
                        f"I noticed a few things. "
                        f"{unique_descriptions[0]}"
                    )

                    for description in unique_descriptions[1:]:

                        # Avoid repeating the date/introduction for every memory.
                        response += (
                            " I also noticed "
                            + description[0].lower()
                            + description[1:]
                        )

            else:

                response = (
                    "I remember capturing some images, "
                    "but I don't have enough details to describe them."
                )

        else:

            response = (
                "I couldn't find anything matching that "
                "in my visual memories."
            )

        print("[EDITH MEMORY] Response:", response)

        respond(response)

        return

    # ==========================================
    # COMMAND 1: GREETING
    # ==========================================

    # ==========================================
    # COMMAND: VISUAL TRACKING MEMORY
    # ==========================================

    event_response = get_visual_event_response(command)

    if event_response is not None:

        print("[EDITH VISUAL MEMORY] Response:", event_response)

        respond(event_response)

        return

    if any(phrase in command for phrase in [
        "hi edith",
        "hello edith",
        "hey edith"
    ]):

        if random.random() < 0.25:
            speak("One moment, please.")

        print("[EDITH] Hello! I'm listening.")
        speak("Hello! I'm listening.")

    # ==========================================
    # COMMAND 2: OBJECT DETECTION
    # ==========================================

    elif any(phrase in command for phrase in [
        "what can you see",
        "what do you see",
        "identify objects",
        "detect objects"
    ]) and not command.startswith("describe"):

        speak("Give me a moment. I'm identifying the objects.")

        print("[EDITH] Looking around...")

        image = capture_image()

        if image:
            detection_result = run_object_detection(image)

            display_results(detection_result)

            # ==================================
            # EDITH SPEAKS DETECTED OBJECTS
            # ==================================

            detections = detection_result["detections"]

            if detections:

                detected_objects = []

                for detection in detections:

                    name = detection["class_name"].replace("_", " ")
                    position = get_object_position(detection)
                    description = f"{name} {position}"

                    if description not in detected_objects:
                        detected_objects.append(description)

                objects_text = ", ".join(detected_objects)

                respond(f"I can see {objects_text}.")

            else:
                print("[EDITH] No objects detected.")
                respond("I couldn't identify any objects.")

        else:
            print("[EDITH] I couldn't capture an image.")
            respond("I couldn't capture an image.")

    # ==========================================
    # COMMAND 3: SCENE DESCRIPTION
    # ==========================================

    # ==========================================

    # COMMAND 3: SCENE DESCRIPTION

    # ==========================================

    elif is_visual_question(command):

        answer_live_visual_question(command)

    else:
        print("[EDITH] I don't recognise that command yet.")
        speak("I don't recognise that command yet.")



def run_text_mode():
    """Run EDITH from the Mac terminal without microphone or ESP32 hardware."""
    os.environ.setdefault("EDITH_TTS_OUTPUT", "local")

    print("\n========================================")
    print("       EDITH MAC DESKTOP MODE")
    print("========================================")
    print("Type a command as if you were speaking to EDITH.")
    print("Set EDITH_IMAGE_PATH in .env to use a local image.")
    print("Type 'exit' to finish.\n")

    try:
        while True:
            try:
                command = input("[YOU] ").strip()
            except EOFError:
                break

            if command.lower() in ("exit", "quit", "stop"):
                break

            if not command:
                continue

            try:
                process_command(command)
            except Exception as error:
                print("[EDITH DESKTOP] Command failed:", error)
                traceback.print_exc()

    except KeyboardInterrupt:
        print("\n[EDITH] Desktop session interrupted.")

    finally:
        close_moondream_session()
        print("[EDITH] Desktop session ended.")



# ==========================================
# WAV RECORDING
# ==========================================

def save_wav(audio_data):

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    wav_path = TEMP_DIR / "edith_current_utterance.wav"

    with wave.open(str(wav_path), "wb") as wav:

        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)

        wav.writeframes(audio_data)

    return wav_path


# ==========================================
# SPEECH RECOGNITION WORKER
# ==========================================


def transcription_worker():
    global last_activity_time

    print("[EDITH] Speech recognition worker started.")

    try:
        import mlx_whisper
        print("[EDITH] Whisper engine ready.")

    except Exception as error:
        print("[EDITH ERROR] Whisper failed to initialise:", error)
        traceback.print_exc()
        return

    while True:


        try:
            audio_data = audio_queue.get(timeout=1)

        except queue.Empty:

            if time.monotonic() - last_activity_time >= IDLE_TIMEOUT:
                speak("I'm here if you need anything.")
                last_activity_time = time.monotonic()

            continue

        if audio_data is None:
            audio_queue.task_done()
            break

        try:

            wav_path = save_wav(audio_data)

            print("\n[EDITH] Transcribing speech...")

            transcription_start = time.time()

            result = mlx_whisper.transcribe(
                str(wav_path),
                path_or_hf_repo=MODEL,
                language="en",
                task="transcribe",
                initial_prompt=(
                    "EDITH is the name of an AI assistant. "
                    "The speaker may say Hi EDITH or Hello EDITH."
                )
            )

            print(
                f"[EDITH] Whisper processing time: "
                f"{time.time() - transcription_start:.2f} seconds"
            )

            text = result.get("text", "").strip()

            for segment in result.get("segments", []):
                print(
                    f"[EDITH] No-speech probability: "
                    f"{segment.get('no_speech_prob', 'N/A')}"
                )

                print(
                    f"[EDITH] Average log probability: "
                    f"{segment.get('avg_logprob', 'N/A')}"
                )

                print(
                    f"[EDITH] Compression ratio: "
                    f"{segment.get('compression_ratio', 'N/A')}"
                )

            print("\n================================")
            print("         EDITH HEARD")
            print("================================")

            if text:
                print(text)
            else:
                print("[EDITH] No speech recognised.")

            print("================================\n")

            if text:

                # Save the user's recognised speech.
                try:
                    vision.memory.save_conversation(
                        role="user",
                        content=text
                    )

                except Exception as error:
                    print("[EDITH MEMORY] Could not save user message:", error)

                # Process the recognised command.
                process_command(text)

                last_activity_time = time.monotonic()

        except Exception as error:

            print("[EDITH] Transcription error:", error)
            traceback.print_exc()

        finally:
            audio_queue.task_done()


# ==========================================
# SILERO VAD PROCESSING
# ==========================================

def detect_speech(vad_model, audio_chunk):

    # Convert 16-bit PCM into normalized float audio.
    samples = struct.unpack(
        "<" + "h" * CHUNK_SAMPLES,
        audio_chunk
    )

    audio_tensor = torch.tensor(
        samples,
        dtype=torch.float32
    ) / 32768.0

    with torch.inference_mode():
        probability = vad_model(
            audio_tensor,
            SAMPLE_RATE
        ).item()

    return probability


# ==========================================
# MAIN LISTENING SYSTEM
# ==========================================

def main(vad_model):
    global last_activity_time

    print("========================================")
    print("       EDITH AUTOMATIC LISTENING")
    print("========================================")

    receiver = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    receiver.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_RCVBUF,
        1024 * 1024
    )

    receiver.bind((HOST, PORT))
    receiver.settimeout(1.0)

    print("[EDITH] Waiting for ESP32 microphone...")

    worker = threading.Thread(
        target=transcription_worker,
        daemon=True
    )

    worker.start()

    print("[EDITH] Silero VAD ready!")
    print("[EDITH] Listening continuously.")
    print("[EDITH] Speak normally. No wake word required.")

    # ======================================
    # LISTENING STATE
    # ======================================

    voice_active = False

    speech_count = 0
    silence_count = 0

    pre_roll = deque(maxlen=PRE_ROLL_CHUNKS)

    current_utterance = []

    packet_buffer = bytearray()

    total_packets = 0

    try:

        while True:

            # Check whether the transcription worker is still running.
            if not worker.is_alive():
                print(
                    "[EDITH ERROR] Transcription worker stopped. "
                    "Stopping the listening system."
                )
                break

            try:
                data, address = receiver.recvfrom(4096)


            except socket.timeout:
                continue

            # Ignore unexpected packet sizes.
            if len(data) != PACKET_BYTES:
                continue

            total_packets += 1

            # Combine two 256-sample packets.
            packet_buffer.extend(data)

            if len(packet_buffer) < CHUNK_BYTES:
                continue

            audio_chunk = bytes(packet_buffer[:CHUNK_BYTES])
            del packet_buffer[:CHUNK_BYTES]

            # ==================================
            # IGNORE AUDIO WHILE EDITH SPEAKS
            # ==================================

            if speaking.is_set():
                voice_active = False

                speech_count = 0
                silence_count = 0

                pre_roll.clear()
                current_utterance = []

                continue

            # ==================================
            # NEURAL SPEECH DETECTION
            # ==================================

            probability = detect_speech(
                vad_model,
                audio_chunk
            )

            # ==================================
            # WAITING FOR SPEECH
            # ==================================

            if not voice_active:

                pre_roll.append(audio_chunk)

                if probability >= START_THRESHOLD:
                    speech_count += 1
                else:
                    speech_count = 0

                if speech_count >= START_CHUNKS:

                    voice_active = True
                    last_activity_time = time.monotonic()

                    current_utterance = list(pre_roll)
                    silence_count = 0

                    print(
                        f"\n[EDITH] Speech detected! "
                        f"Probability: {probability:.2f}"
                    )

            # ==================================
            # RECORDING SPEECH
            # ==================================

            else:

                current_utterance.append(audio_chunk)

                if probability < STOP_THRESHOLD:
                    silence_count += 1
                else:
                    silence_count = 0

                # Finish after sufficient silence.
                if silence_count >= STOP_CHUNKS:

                    voice_active = False

                    complete_audio = b"".join(current_utterance)

                    duration = (
                        len(complete_audio)
                        / (SAMPLE_RATE * SAMPLE_WIDTH)
                    )

                    print(
                        f"[EDITH] Recording complete: "
                        f"{duration:.2f} seconds"
                    )

                    try:
                        audio_queue.put_nowait(complete_audio)

                    except queue.Full:
                        print(
                            "[EDITH WARNING] Audio queue full. "
                            "Discarding the oldest pending recording."
                        )

                        try:
                            audio_queue.get_nowait()
                            audio_queue.task_done()
                        except queue.Empty:
                            pass

                        try:
                            audio_queue.put_nowait(complete_audio)
                        except queue.Full:
                            print("[EDITH WARNING] Could not queue the latest recording.")

                    current_utterance = []

                    speech_count = 0
                    silence_count = 0

                    pre_roll.clear()

                    print(
                        "[EDITH] Listening for your next command..."
                    )

    except KeyboardInterrupt:
        print("\n[EDITH] Shutting down...")


    finally:

        receiver.close()

        # Submit any unfinished recording.

        if current_utterance:

            remaining_audio = b"".join(current_utterance)

            if len(remaining_audio) >= CHUNK_BYTES:
                audio_queue.put(remaining_audio)

        audio_queue.put(None)

        worker.join()

        print("[EDITH] Audio system stopped.")


# ==========================================
# START EDITH
# ==========================================

if __name__ == "__main__":

    print("[EDITH] Loading Silero VAD...")

    vad_model = load_silero_vad()
    vad_model.eval()

    print("[EDITH] Silero VAD loaded successfully!")
    print("[EDITH] Device: CPU")

    main(vad_model)
