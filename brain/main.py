
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
