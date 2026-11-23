
import sqlite3
import json
import uuid

from pathlib import Path
from datetime import datetime


# ==========================================
# EDITH - LONG TERM MEMORY SYSTEM
# ==========================================

# Project directories

BASE_DIR = Path(__file__).resolve().parent.parent

MEMORY_DIR = BASE_DIR / "memory"
IMAGE_DIR = MEMORY_DIR / "images"

DATABASE_PATH = MEMORY_DIR / "edith_memory.db"


# ==========================================
# CREATE MEMORY DIRECTORIES
# ==========================================

MEMORY_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================
# DATABASE INITIALISATION
# ==========================================


def initialise_database():

    connection = get_connection()

    try:

        cursor = connection.cursor()

        # --------------------------------------
        # VISUAL MEMORIES TABLE
        # --------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS visual_memories (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                captured_at TEXT NOT NULL,

                image_path TEXT NOT NULL,

                scene_description TEXT,

                detections TEXT,

                source TEXT

            )
        """)

        # --------------------------------------
        # VISUAL MEMORY TIMELINE INDEX
        # --------------------------------------

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_visual_memories_timestamp

            ON visual_memories(captured_at)
        """)

        # --------------------------------------
        # CONVERSATION MEMORY TABLE
        # --------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                timestamp TEXT NOT NULL,

                role TEXT NOT NULL,

                content TEXT NOT NULL,

                visual_memory_id INTEGER,

                FOREIGN KEY (visual_memory_id)
                    REFERENCES visual_memories(id)

            )
        """)

        # --------------------------------------
        # CONVERSATION TIMELINE INDEX
        # --------------------------------------

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_conversations_timestamp

            ON conversations(timestamp)
        """)

        # --------------------------------------
        # EXPLICIT MEMORIES TABLE
        # --------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS explicit_memories (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                timestamp TEXT NOT NULL,

                content TEXT NOT NULL

            )
        """)

        # --------------------------------------
        # EXPLICIT MEMORY TIMELINE INDEX
        # --------------------------------------

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_explicit_memories_timestamp

            ON explicit_memories(timestamp)
        """)

        # Commit only after ALL tables and indexes
        # have been successfully created.

        connection.commit()

        print("[EDITH MEMORY] Database initialised.")

    except Exception:

        connection.rollback()

        print("[EDITH MEMORY] Database initialisation failed.")

        raise

    finally:

        connection.close()



# ==========================================
# DATABASE CONNECTION
# ==========================================


def get_connection():

    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=10
    )

    # Wait up to 10 seconds for another database operation.
    connection.execute("PRAGMA busy_timeout = 10000")

    # Enforce foreign-key relationships.
    connection.execute("PRAGMA foreign_keys = ON")

    return connection



# ==========================================
# SAVE IMAGE TO LONG-TERM MEMORY
# ==========================================


def save_visual_memory(image_bytes, source="automatic"):

    if not image_bytes:
        print("[EDITH MEMORY] No image received.")
        return None

    image_uuid = str(uuid.uuid4())

    timestamp = datetime.now().astimezone().isoformat()

    image_filename = f"{image_uuid}.jpg"
    image_path = IMAGE_DIR / image_filename

    connection = None
    image_saved = False

    try:

        # Save the image first.
        image_path.write_bytes(image_bytes)
        image_saved = True

        connection = get_connection()
        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO visual_memories (
                captured_at,
                image_path,
                source
            )
            VALUES (?, ?, ?)
        """, (
            timestamp,
            str(image_path.relative_to(BASE_DIR)),
            source
        ))

        database_id = cursor.lastrowid

        connection.commit()

        print(
            f"[EDITH MEMORY] Visual memory saved: {database_id}"
        )

        return database_id

    except Exception as error:

        print(
            f"[EDITH MEMORY] Failed to save image: {error}"
        )

        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass

        if image_saved:

            try:
                image_path.unlink(missing_ok=True)

            except OSError as cleanup_error:

                print(
                    "[EDITH MEMORY] Image cleanup failed:",
                    cleanup_error
                )

        return None

    finally:

        if connection is not None:
            connection.close()



# ==========================================
# RETRIEVE RECENT VISUAL MEMORIES
# ==========================================

def get_recent_memories(limit=5):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT id, captured_at, image_path, scene_description
        FROM visual_memories
        ORDER BY captured_at DESC
        LIMIT ?
    """, (limit,))

    memories = cursor.fetchall()

    connection.close()

    return memories

def search_visual_memories(query, limit=5):
    """
    Search visual memories using keywords from
    scene descriptions and YOLO detections.
    """

    if not query or not query.strip():
        return []

    # Remove common words that aren't useful for searching.
    stop_words = {
        "what", "where", "when", "who", "why",
        "how", "the", "a", "an", "is", "are",
        "was", "were", "do", "did", "does",
        "you", "i", "me", "my", "your",
        "can", "could", "would", "should",
        "remember", "see", "saw", "there",
        "earlier", "before", "this", "that",
        "it", "in", "on", "at", "of", "to"
    }

    words = {
        word.strip(".,!?;:\"'()[]{}").lower()
        for word in query.split()
    }

    keywords = [
        word for word in words
        if word and word not in stop_words
    ]

    if not keywords:
        return []

    connection = get_connection()

    try:
        cursor = connection.cursor()

        # Search scene descriptions and detection data.
        conditions = []
        parameters = []

        for keyword in keywords:
            conditions.append(
                "(scene_description LIKE ? OR detections LIKE ?)"
            )

            parameters.extend([
                f"%{keyword}%",
                f"%{keyword}%"
            ])

        sql = f"""
            SELECT
                id,
                captured_at,
                image_path,
                scene_description,
                detections,
                source
            FROM visual_memories
            WHERE {" OR ".join(conditions)}
            ORDER BY captured_at DESC
        """

        cursor.execute(sql, parameters)

        rows = cursor.fetchall()

        results = []

        for row in rows:

            memory_data = {
                "id": row[0],
                "captured_at": row[1],
                "image_path": row[2],
                "scene_description": row[3],
                "detections": json.loads(row[4]) if row[4] else [],
                "source": row[5]
            }

            # Calculate how relevant this memory is.
            searchable_text = (
                (row[3] or "") + " " +
                (row[4] or "")
            ).lower()

            score = sum(
                1 for keyword in keywords
                if keyword in searchable_text
            )

            memory_data["relevance"] = score

            results.append(memory_data)

        # Most relevant memories appear first.
        results.sort(
            key=lambda item: item["relevance"],
            reverse=True
        )

        return results[:max(0, int(limit))]

    finally:
        connection.close()


# ==========================================
# UPDATE VISUAL MEMORY
# ==========================================


def update_visual_memory(memory_id, scene_description=None, detections=None):

    updates = []
    values = []

    if scene_description is not None:
        updates.append("scene_description = ?")
        values.append(scene_description)

    if detections is not None:
        updates.append("detections = ?")
        values.append(json.dumps(detections))

    if not updates:
        return False

    values.append(memory_id)

    query = f"""
        UPDATE visual_memories
        SET {", ".join(updates)}
        WHERE id = ?
    """

    connection = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        cursor.execute(query, values)

        updated = cursor.rowcount > 0

        connection.commit()

        return updated

    except Exception as error:

        print(
            "[EDITH MEMORY] Failed to update visual memory:",
            error
        )

        if connection is not None:
            connection.rollback()

        return False

    finally:

        if connection is not None:
            connection.close()


# ==========================================
# GET A SPECIFIC VISUAL MEMORY
# ==========================================

def get_visual_memory(memory_id):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT id, captured_at, image_path,
               scene_description, detections, source
        FROM visual_memories
        WHERE id = ?
    """, (memory_id,))

    memory_record = cursor.fetchone()

    connection.close()

    if memory_record is None:
        return None

    return {
        "id": memory_record[0],
        "captured_at": memory_record[1],
        "image_path": memory_record[2],
        "scene_description": memory_record[3],
        "detections": json.loads(memory_record[4]) if memory_record[4] else [],
        "source": memory_record[5]
    }


# ==========================================
# SAVE CONVERSATION MEMORY
# ==========================================

def save_conversation(role, content, visual_memory_id=None):

    if not content or not content.strip():
        return None

    if role not in ("user", "assistant"):
        raise ValueError("Invalid conversation role.")

    timestamp = datetime.now().astimezone().isoformat()

    connection = get_connection()

    cursor = connection.cursor()

    try:

        cursor.execute("""
            INSERT INTO conversations (
                timestamp,
                role,
                content,
                visual_memory_id
            )
            VALUES (?, ?, ?, ?)
        """, (
            timestamp,
            role,
            content.strip(),
            visual_memory_id
        ))

        connection.commit()

        conversation_id = cursor.lastrowid

        print(
            f"[EDITH MEMORY] Conversation saved: "
            f"{conversation_id} ({role})"
        )

        return conversation_id

    finally:
        connection.close()


# ==========================================
# RETRIEVE RECENT CONVERSATIONS
# ==========================================

def get_recent_conversations(limit=20):

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute("""
        SELECT id, timestamp, role, content, visual_memory_id
        FROM conversations
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
    """, (limit,))

    conversations = cursor.fetchall()

    connection.close()

    return [
        {
            "id": row[0],
            "timestamp": row[1],
            "role": row[2],
            "content": row[3],
            "visual_memory_id": row[4]
        }
        for row in reversed(conversations)
    ]



def save_explicit_memory(content):

    if not content or not content.strip():
        return None

    timestamp = datetime.now().astimezone().isoformat()

    connection = None

    try:

        connection = get_connection()

        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO explicit_memories (timestamp, content)
            VALUES (?, ?)
        """, (timestamp, content.strip()))

        memory_id = cursor.lastrowid

        connection.commit()

        print(
            f"[EDITH MEMORY] Explicit memory saved: {memory_id}"
        )

        return memory_id

    except Exception as error:

        print(
            "[EDITH MEMORY] Failed to save explicit memory:",
            error
        )

        if connection is not None:
            connection.rollback()

        return None

    finally:

        if connection is not None:
            connection.close()




def search_explicit_memories(query, limit=5):

    if not isinstance(query, str) or not query.strip():
        return []

    stop_words = {
        "where", "when", "what", "who", "why", "how",
        "did", "do", "does", "is", "are", "was", "were",
        "i", "my", "me", "you", "your", "the", "a", "an",
        "to", "of", "in", "on", "at", "inside", "under",
        "and", "or", "that", "this", "it",
        "keep", "kept", "put", "leave", "left",
        "have", "has", "had", "can", "could",
        "remember", "tell", "told", "ask", "asked"
    }

    query_words = [
        word.strip(".,!?;:'\"()[]{}").lower()
        for word in query.split()
    ]

    query_words = [
        word for word in query_words
        if word and word not in stop_words
    ]

    if not query_words:
        return []

    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        return []

    if limit == 0:
        return []

    connection = None

    try:

        connection = get_connection()

        rows = connection.execute("""
            SELECT id, timestamp, content
            FROM explicit_memories
        """).fetchall()

        results = []

        for row in rows:

            memory_id, timestamp, content = row

            content_lower = content.lower()

            matching_words = [
                word for word in query_words
                if word in content_lower
            ]

            relevance = len(set(matching_words))

            if relevance > 0:

                results.append({
                    "id": memory_id,
                    "timestamp": timestamp,
                    "content": content,
                    "relevance": relevance
                })

        # Prioritise relevance, then the newest memory.
        results.sort(
            key=lambda item: (
                item["relevance"],
                item["timestamp"]
            ),
            reverse=True
        )

        return results[:limit]

    except Exception as error:

        print(
            "[EDITH MEMORY] Explicit memory search failed:",
            error
        )

        return []

    finally:

        if connection is not None:
            connection.close()





# ==========================================
# MEMORY SYSTEM STARTUP
# ==========================================


if __name__ == "__main__":
    print("=" * 45)
    print("       EDITH MEMORY SYSTEM")
    print("=" * 45)

    initialise_database()

    # Create a test image
    from PIL import Image
    from io import BytesIO

    test_image = Image.new(
        "RGB",
        (640, 480),
        color="blue"
    )

    buffer = BytesIO()

    test_image.save(buffer, format="JPEG")

    image_bytes = buffer.getvalue()

    # Save test image
    memory_id = save_visual_memory(
        image_bytes,
        source="test"
    )

    if memory_id:
        print("\n[EDITH MEMORY] Test successful!")

    # Retrieve recent memories
    memories = get_recent_memories()

    print(f"\nTotal retrieved memories: {len(memories)}")

    for memory in memories:
        print(memory)

    # Test visual understanding storage

    test_detections = [
        {
            "class_name": "person",
            "confidence": 0.95,
            "position": "centre"
        },
        {
            "class_name": "chair",
            "confidence": 0.87,
            "position": "left"
        }
    ]

    success = update_visual_memory(
        memory_id,
        scene_description="A person standing beside a chair.",
        detections=test_detections
    )

    print("\nMemory update successful:", success)

    # Retrieve the updated memory

    saved_memory = get_visual_memory(memory_id)

    print("\nRetrieved visual memory:")
    print(saved_memory)


    # ======================================
    # TEST CONVERSATIONAL MEMORY
    # ======================================

    print("\n[EDITH MEMORY] Testing conversations...")

    save_conversation(
        "user",
        "EDITH, what can you see?"
    )

    save_conversation(
        "assistant",
        "I can see a person standing beside a chair."
    )

    conversations = get_recent_conversations(limit=10)

    print("\nRecent conversations:")

    for conversation in conversations:
        print(
            f"[{conversation['role'].upper()}] "
            f"{conversation['content']}"
        )

    # ======================================
    # TEST VISUAL MEMORY SEARCH
    # ======================================

    print("\n[EDITH MEMORY] Testing visual search...")

    search_results = search_visual_memories(
        "Do you remember the chair?",
        limit=5
    )

    print("\nSearch results:")

    for result in search_results:

        print(
            f"Memory ID: {result['id']} | "
            f"Relevance: {result['relevance']}"
        )

        print(
            f"Description: {result['scene_description']}"
        )

        print()

    print("\n[EDITH MEMORY] Testing explicit memories...")

    #explicit_id = save_explicit_memory(
    #    "I keep my spare keys inside the top drawer of my desk."
    #)

    results = search_explicit_memories("Where are my spare keys?")

    for item in results:
        print(
            f"ID: {item['id']} | "
            f"Memory: {item['content']}"
        )


