# EDITH Gemini Live Vision (Experimental)

This is an optional, separate vision backend. It does not replace or modify
EDITH's YOLO11, Moondream, audio, or SQLite memory pipelines.

## What it does

- Reads the ESP32-CAM MJPEG stream directly from `/stream`.
- Keeps only the newest camera frame in a one-frame queue, avoiding a backlog.
- Sends JPEG frames to Gemini Live at approximately 1 frame per second (the
  Live API video-input limit).
- Accepts typed questions in the terminal and prints Gemini's text responses.
- Uses the existing `CAMERA_URL` in `.env` to derive the stream address,
  unless `CAMERA_STREAM_URL` is explicitly configured.

Video frames alone do not necessarily trigger a response. Ask a question in
EDITH's terminal; Gemini answers using the live frames already sent in the
session.

## Setup

From the project root, with the existing `.venv312` environment activated:

```bash
pip install -r requirements-gemini.txt
```

Add your Gemini API key to the local `.env` file (never commit the key):

```dotenv
GEMINI_API_KEY=your_google_ai_studio_api_key
```

The module uses the existing `CAMERA_URL` setting to infer the stream address.
If you prefer to set it explicitly, add:

```dotenv
CAMERA_STREAM_URL=http://YOUR_ESP32_CAM_IP:81/stream
```

Optional model override:

```dotenv
GEMINI_LIVE_MODEL=gemini-3.8-live
```

## Run

With the ESP32-CAM connected to the same network and its V1.3 stream server
running, execute from the project root:

```bash
python -m brain.gemini_live
```

Type a question and press Enter. Type `exit` to stop. Ctrl+C also requests
shutdown.

## Notes and limitations

- This first version is text-in/text-out at the terminal. It does not connect
  to EDITH's existing microphone or speaker pipeline.
- The camera itself can stream faster; only the frames sent to Gemini are
  throttled to the Live API's approximately 1 FPS video limit.
- Gemini Live is a preview API and model availability, limits, and SDK behavior
  can change. Verify the selected model is enabled for the API key's project.
- This integration has not been end-to-end tested against the user's camera or
  Gemini API credentials yet.
