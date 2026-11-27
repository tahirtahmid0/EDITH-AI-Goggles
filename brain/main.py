
import os
import traceback

from silero_vad import load_silero_vad

import audio
import memory

# ==========================================
# EDITH - CENTRAL CONTROLLER
# ==========================================


def main():

    print("========================================")
    print("             EDITH AI")
    print("========================================")

    startup_complete = False

    try:

        # ==================================
        # INITIALISE MEMORY DATABASE
        # ==================================

        print("\n[EDITH] Initialising memory system...")

        memory.initialise_database()

        print("[EDITH] Memory system ready!")

        # ==================================
        # MAC-ONLY DESKTOP DEVELOPMENT MODE
        # ==================================

        desktop_mode = os.getenv(
            "EDITH_DESKTOP_MODE", ""
        ).strip().lower() in ("1", "true", "yes", "on")

        if desktop_mode:
            os.environ.setdefault("EDITH_TTS_OUTPUT", "local")
            startup_complete = True
            print("\n[EDITH] Starting Mac-only text command mode.")
            print("[EDITH] No microphone, ESP32-CAM or speaker is required.")
            audio.run_text_mode()
            return

        # ==================================
        # INITIALISE SPEECH DETECTION
        # ==================================

        print("\n[EDITH] Initialising speech detection...")

        vad_model = load_silero_vad()
        vad_model.eval()

        print("[EDITH] Silero VAD loaded successfully!")

        # ==================================
        # START EDITH
        # ==================================

        print("\n[EDITH] All systems initialised.")

        startup_complete = True

        print("[EDITH] Starting automatic listening...")

        audio.main(vad_model)

    except KeyboardInterrupt:

        print("\n[EDITH] Shutdown requested.")

    except Exception:

        print("\n[EDITH ERROR] EDITH encountered an unexpected error.")
        traceback.print_exc()

    finally:

        # Keep cleanup here as a final safety net, including failures that
        # happen before the audio listener has entered its own try/finally.
        try:
            audio.close_gemini_live()
        except Exception as error:
            print("[EDITH SHUTDOWN] Gemini cleanup failed:", error)

        try:
            audio.close_moondream_session()
        except Exception as error:
            print("[EDITH SHUTDOWN] Moondream cleanup failed:", error)

        if startup_complete:
            print("\n[EDITH] Session ended.")
        else:
            print("\n[EDITH] Startup did not complete.")

        print("\n========================================")
        print("          EDITH SHUTDOWN")
        print("========================================")



# ==========================================
# PROGRAM ENTRY POINT
# ==========================================

if __name__ == "__main__":
    main()
