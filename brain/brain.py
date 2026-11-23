import os
import time
import gc
import requests
import torch
import sys
import traceback

from io import BytesIO
from pathlib import Path
from PIL import Image
from dotenv import load_dotenv
from ultralytics import YOLO
from transformers import AutoModelForCausalLM


# ==========================================
# EDITH - VISION AI
# ESP32-CAM + YOLO11n + Moondream 2
# ==========================================

BRAIN_DIR = Path(__file__).resolve().parent

if str(BRAIN_DIR) not in sys.path:
    sys.path.insert(0, str(BRAIN_DIR))

import memory

BASE_DIR = Path(__file__).resolve().parent.parent

MODELS_DIR = BASE_DIR / "models"
TEMP_DIR = BASE_DIR / "temp"

YOLO_MODEL_PATH = MODELS_DIR / "yolo" / "yolo11s.pt"
INPUT_PATH = TEMP_DIR / "edith_input.jpg"

# ==========================================
# EDITH VISUAL MEMORY STATE
# ==========================================

CURRENT_MEMORY_ID = None

MOONDREAM_MODEL_ID = "vikhyatk/moondream2"
MOONDREAM_REVISION = "5d6c926f44e26b07957b0dd315bbedcb4c17a5fe"

MOONDREAM_SETTINGS = {
    "max_tokens": 128,
    "temperature": 0.0,
    "top_p": 1.0,
}

CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

YOLO_CONFIDENCE = 0.30
YOLO_IMAGE_SIZE = 640

load_dotenv(BASE_DIR / ".env")

CAMERA_URL = os.getenv("CAMERA_URL")

# Persistent HTTP connection to the ESP32-CAM.
CAMERA_SESSION = requests.Session()

CAMERA_SESSION.headers.update({
    "Connection": "keep-alive"
})

TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================
# DEVICE MANAGEMENT
# ==========================================

def get_device():

    if torch.backends.mps.is_available():
        return "mps"

    return "cpu"


def synchronize_device(device):

    if device == "mps":
        torch.mps.synchronize()


# ==========================================
# MEMORY MANAGEMENT
# ==========================================

def release_torch_memory(device):

    gc.collect()

    if device == "mps":
        torch.mps.empty_cache()


# ==========================================
# CAMERA CAPTURE
# ==========================================



def capture_image():

    global CURRENT_MEMORY_ID

    CURRENT_MEMORY_ID = None

    print("\n[EDITH] Connecting to camera...")

    if not CAMERA_URL:
        raise ValueError("CAMERA_URL is missing from .env")

    image_bytes = None

    # Allow the ESP32-CAM multiple attempts to respond.
    MAX_ATTEMPTS = 3

    for attempt in range(1, MAX_ATTEMPTS + 1):

        try:

            print(
                f"[EDITH] Camera connection attempt "
                f"{attempt}/{MAX_ATTEMPTS}..."
            )

            start_time = time.perf_counter()

            with CAMERA_SESSION.get(
                    CAMERA_URL,
                    timeout=(5, 15),
                    stream=True
            ) as response:

                response.raise_for_status()

                image_bytes = response.content

            elapsed = time.perf_counter() - start_time

            transfer_speed = (
                len(image_bytes) / elapsed / 1024
                if elapsed > 0 else 0
            )

            print(f"[EDITH] Transfer time: {elapsed:.3f} seconds.")
            print(f"[EDITH] Transfer speed: {transfer_speed:.2f} KB/s.")

            # Validate the received JPEG.
            if not image_bytes.startswith(b"\xff\xd8"):
                raise ValueError("Camera did not return a valid JPEG.")

            with Image.open(BytesIO(image_bytes)) as image:
                image.verify()

            print(
                f"[EDITH] Camera responded in "
                f"{elapsed:.2f} seconds."
            )

            break


        except (

                requests.exceptions.RequestException,

                ValueError,

                OSError

        ) as error:

            print(f"[EDITH] Camera attempt {attempt} failed: {error}")

            if attempt == MAX_ATTEMPTS:
                raise

            print("[EDITH] Retrying in 2 seconds...")
            time.sleep(2)

    if image_bytes is None:
        raise RuntimeError("Could not capture an image from the camera.")

    # Read image dimensions.
    with Image.open(BytesIO(image_bytes)) as image:
        width, height = image.size

    # Save temporary image for the vision pipeline.
    INPUT_PATH.write_bytes(image_bytes)

    # Save permanent copy to EDITH's memory.
    # Save a permanent copy to EDITH's memory.
    # A memory failure must not stop the vision pipeline.

    try:

        CURRENT_MEMORY_ID = memory.save_visual_memory(
            image_bytes,
            source="camera"
        )

    except Exception as error:

        CURRENT_MEMORY_ID = None

        print(
            "[EDITH MEMORY] Could not save visual memory:",
            error
        )

    if CURRENT_MEMORY_ID is not None:
        print(f"[EDITH MEMORY] Memory ID: {CURRENT_MEMORY_ID}")
    else:
        print("[EDITH MEMORY] Image was not archived.")

    print("[EDITH] Camera connected!")
    print(f"[EDITH] Image dimensions: {width} x {height}")
    print(f"[EDITH] Image size: {len(image_bytes)} bytes")
    print(f"[EDITH] Saved: {INPUT_PATH}")

    return image_bytes


# ==========================================
# YOLO OBJECT DETECTION
# ==========================================

def detect_objects(image):

    print("\n[EDITH] YOLO object detection stage")

    if not YOLO_MODEL_PATH.exists():
        raise FileNotFoundError(
            f"YOLO model not found: {YOLO_MODEL_PATH}"
        )

    device = get_device()

    print("[EDITH] Selected device:", device)
    print("[EDITH] Loading YOLO11s...")

    model = None
    results = None
    pil_image = None

    try:

        model = YOLO(str(YOLO_MODEL_PATH))

        # Fix THOP / Python standard-library import collision.
        import thop

        thop_directory = str(Path(thop.__file__).resolve().parent)

        sys.path[:] = [
            path for path in sys.path
            if os.path.realpath(path or os.getcwd()) != thop_directory
        ]

        print("[EDITH] Python import paths checked.")

        with Image.open(BytesIO(image)) as source:
            pil_image = source.convert("RGB")

        print("[EDITH] Running object detection...")

        synchronize_device(device)

        start_time = time.perf_counter()

        results = model.predict(
            source=pil_image,
            device=device,
            imgsz=YOLO_IMAGE_SIZE,
            conf=YOLO_CONFIDENCE,
            verbose=False
        )

        synchronize_device(device)

        processing_time = time.perf_counter() - start_time

        detections = []

        for result in results:

            boxes = result.boxes

            if boxes is None or len(boxes) == 0:
                continue

            # Transfer detection tensors to CPU together.
            coordinates = boxes.xyxy.cpu().tolist()
            confidences = boxes.conf.cpu().tolist()
            class_ids = boxes.cls.cpu().tolist()

            for bbox, confidence, class_id in zip(
                coordinates,
                confidences,
                class_ids
            ):

                x1, y1, x2, y2 = map(int, bbox)

                class_id = int(class_id)

                detections.append({
                    "class_name": model.names[class_id],
                    "confidence": round(float(confidence), 3),
                    "bbox": [x1, y1, x2, y2]
                })



        # Save YOLO detections to visual memory.
        # Database errors must not interrupt object detection.

        if CURRENT_MEMORY_ID is not None:

            try:

                updated = memory.update_visual_memory(
                    CURRENT_MEMORY_ID,
                    detections=detections
                )

                if updated:
                    print("[EDITH MEMORY] YOLO detections saved.")
                else:
                    print("[EDITH MEMORY] Could not update YOLO detections.")

            except Exception as error:

                print(
                    "[EDITH MEMORY] Failed to save YOLO detections:",
                    error
                )



        print(
            f"[EDITH] Detected {len(detections)} objects "
            f"in {processing_time:.3f} seconds."
        )

        return {
            "detection_count": len(detections),
            "detections": detections,
            "processing_time": round(processing_time, 3),
            "device": device
        }

    finally:

        # Release GPU-backed results before clearing memory.
        results = None
        pil_image = None
        model = None
        result = None
        boxes = None

        release_torch_memory(device)

        print("[EDITH] YOLO memory released.")


# ==========================================
# MOONDREAM SESSION
# ==========================================

class MoondreamSession:

    def __init__(self):

        self.device = get_device()

        self.dtype = (
            torch.float16
            if self.device == "mps"
            else torch.float32
        )

        self.model = None
        self.encoded_image = None
        self.current_image = None

    # --------------------------------------
    # LOAD MODEL
    # --------------------------------------

    def load_model(self):

        if self.model is not None:
            return

        print("\n[EDITH] Loading Moondream 2...")
        print("[EDITH] Device:", self.device)
        print("[EDITH] Data type:", self.dtype)

        start_time = time.perf_counter()

        # ==========================================
        # FIX THOP / PYTHON PROFILE IMPORT CONFLICT
        # ==========================================

        import thop

        thop_directory = str(
            Path(thop.__file__).resolve().parent
        )

        sys.path[:] = [
            path for path in sys.path
            if os.path.realpath(path or os.getcwd()) != thop_directory
        ]

        # Remove an incorrectly loaded profile module, if present.
        profile_module = sys.modules.get("profile")

        if profile_module is not None:

            profile_path = getattr(profile_module, "__file__", "")

            if profile_path and thop_directory in str(Path(profile_path).resolve()):
                del sys.modules["profile"]

        print("[EDITH] Python import paths checked before Moondream loading.")

        self.model = AutoModelForCausalLM.from_pretrained(
            MOONDREAM_MODEL_ID,
            revision=MOONDREAM_REVISION,
            trust_remote_code=True,
            dtype=self.dtype,
            device_map={"": self.device}
        )

        loading_time = time.perf_counter() - start_time

        print(
            f"[EDITH] Moondream loaded in "
            f"{loading_time:.2f} seconds."
        )

    # --------------------------------------
    # ENCODE IMAGE
    # --------------------------------------

    def encode_image(self, image):

        if (
            self.current_image == image
            and self.encoded_image is not None
        ):

            print("\n[EDITH] Reusing previously encoded image.")
            return

        print("\n[EDITH] Encoding camera image...")

        synchronize_device(self.device)

        start_time = time.perf_counter()


        with Image.open(BytesIO(image)) as source:
            pil_image = source.convert("RGB")

        image_load_time = time.perf_counter() - start_time

        synchronize_device(self.device)

        encode_start = time.perf_counter()

        try:

            encoded_image = self.model.encode_image(pil_image)

        finally:

            pil_image.close()

        synchronize_device(self.device)


        encoding_time = time.perf_counter() - encode_start

        self.encoded_image = encoded_image
        self.current_image = image

        print(
            f"[EDITH] Image preparation: "
            f"{image_load_time:.3f} seconds."
        )

        print(
            f"[EDITH] Model encoding: "
            f"{encoding_time:.3f} seconds."
        )

        print(
            f"[EDITH] Total encoding: "
            f"{image_load_time + encoding_time:.3f} seconds."
        )

    # --------------------------------------
    # ASK QUESTION
    # --------------------------------------

    def ask(self, image, question):

        if not question or not question.strip():
            raise ValueError("Question cannot be empty.")

        self.load_model()

        self.encode_image(image)

        print("\n[EDITH] Moondream is thinking...")

        synchronize_device(self.device)

        start_time = time.perf_counter()

        result = self.model.query(
            self.encoded_image,
            question.strip(),
            stream=False,
            settings=MOONDREAM_SETTINGS
        )

        synchronize_device(self.device)

        answering_time = time.perf_counter() - start_time

        answer = result.get("answer", str(result))

        print("\n================================")
        print("       EDITH MOONDREAM")
        print("================================")
        print("Question:", question)
        print("Answer:", answer)
        print(f"Answering time: {answering_time:.2f} seconds")
        print("================================")

        return answer

    # --------------------------------------
    # RELEASE MODEL
    # --------------------------------------

    def close(self):

        print("\n[EDITH] Releasing Moondream memory...")

        self.encoded_image = None
        self.current_image = None

        self.model = None

        release_torch_memory(self.device)

        print("[EDITH] Moondream memory released.")


# ==========================================
# SCENE UNDERSTANDING
# ==========================================


def understand_scene(image, question=None, session=None):

    if session is None:
        raise ValueError("Moondream session is not available.")

    if not question or not question.strip():
        raise ValueError("A question must be provided.")

    answer = session.ask(image, question)

    # Only save actual scene descriptions.
    # Arbitrary questions should not overwrite the scene description.

    if (
        CURRENT_MEMORY_ID is not None
        and "describe the scene" in question.lower()
    ):

        try:

            updated = memory.update_visual_memory(
                CURRENT_MEMORY_ID,
                scene_description=answer
            )

            if updated:
                print("[EDITH MEMORY] Scene description saved.")
            else:
                print("[EDITH MEMORY] Could not save scene description.")

        except Exception as error:

            print(
                "[EDITH MEMORY] Failed to save scene description:",
                error
            )


    return {
        "scene_description": answer
    }



# ==========================================
# INTERACTIVE VISUAL QUESTION SESSION
# ==========================================

def interactive_visual_session(image):

    print("\n================================")
    print("     EDITH VISUAL QUESTION MODE")
    print("================================")

    print("Ask questions about the captured image.")
    print("Type 'exit' to finish this session.")
    print("Moondream loads only when needed.")

    session = MoondreamSession()

    try:

        while True:

            question = input("\n[YOU] Ask EDITH: ").strip()

            if question.lower() in ("exit", "quit", "stop"):
                break

            if not question:
                continue

            try:

                result = understand_scene(
                    image=image,
                    question=question,
                    session=session
                )

            except Exception as error:

                print("\n[EDITH] Moondream error:", error)

    finally:

        session.close()

        print("[EDITH] Visual session ended.")


# ==========================================
# DISPLAY RESULTS
# ==========================================

def display_results(detection_result):

    print("\n================================")
    print("       EDITH VISION RESULTS")
    print("================================")

    print(
        "\nObjects detected:",
        detection_result["detection_count"]
    )

    print(
        "Detection device:",
        detection_result["device"]
    )

    print(
        "Detection time:",
        detection_result["processing_time"],
        "seconds"
    )

    for item in detection_result["detections"]:

        print("\nObject:", item["class_name"])
        print("Confidence:", item["confidence"])
        print("Bounding box:", item["bbox"])

    print("\n================================")


# ==========================================
# MAIN VISION PIPELINE
# ==========================================

def main():

    print("\n================================")
    print("        EDITH VISION AI")
    print("================================")

    try:

        # 1. Capture camera image
        original_image = capture_image()

        # 2. Run YOLO object detection
        detection_result = detect_objects(original_image)

        # 3. Display detection results
        display_results(detection_result)

        # 4. Optional Moondream session
        print("\n[EDITH] YOLO analysis completed.")

        start_session = input(
            "\n[EDITH] Would you like to ask about this image? (y/n): "
        ).strip().lower()

        if start_session in ("y", "yes"):

            interactive_visual_session(original_image)

        else:

            print("[EDITH] Skipping Moondream.")
            print("[EDITH] No Moondream model was loaded.")

        print("\n[EDITH] Vision pipeline completed!")

    except requests.exceptions.Timeout:
        print("[ERROR] Camera request timed out.")

    except requests.exceptions.ConnectionError as error:
        print("[ERROR] Camera connection problem:", error)

    except requests.exceptions.HTTPError as error:
        print("[ERROR] Camera HTTP error:", error)

    except Exception as error:
        print("\n[ERROR] Vision pipeline failed:")
        traceback.print_exc()


if __name__ == "__main__":
    main()
