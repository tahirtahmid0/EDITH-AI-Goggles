"""Offline tests for EDITH's visual event history search."""

import json
import tempfile
import unittest
from pathlib import Path

from brain.visual_event_memory import (
    get_recent_events,
    load_events,
    search_events,
)


class VisualEventMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "events.jsonl"
        self.events = [
            {
                "timestamp": "2026-11-26T00:00:00+00:00",
                "session_id": "session-a",
                "event": "object_appeared",
                "track_id": 1,
                "class_name": "person",
                "visual_identity_id": "V0001",
            },
            {
                "timestamp": "2026-11-26T00:00:01+00:00",
                "session_id": "session-a",
                "event": "object_moved",
                "track_id": 1,
                "class_name": "person",
                "visual_identity_id": "V0001",
            },
            {
                "timestamp": "2026-11-26T00:00:02+00:00",
                "session_id": "session-b",
                "event": "object_appeared",
                "track_id": 8,
                "class_name": "bottle",
                "visual_identity_id": "V0002",
            },
        ]
        self.path.write_text(
            "".join(json.dumps(event) + "\n" for event in self.events),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_events_preserves_chronological_order(self):
        self.assertEqual(load_events(self.path), self.events)

    def test_recent_events_are_newest_first(self):
        self.assertEqual(
            get_recent_events(2, self.path),
            list(reversed(self.events[-2:])),
        )

    def test_search_by_object_class(self):
        results = search_events(class_name="PERSON", path=self.path)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["class_name"] == "person" for item in results))

    def test_search_by_event_type_and_identity(self):
        results = search_events(
            event_type="object_moved",
            visual_identity_id="V0001",
            path=self.path,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["event"], "object_moved")

    def test_search_by_keyword(self):
        results = search_events("bottle", path=self.path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["class_name"], "bottle")

    def test_session_filter_prevents_cross_session_collision(self):
        results = search_events(
            visual_identity_id="V0001",
            session_id="session-b",
            path=self.path,
        )
        self.assertEqual(results, [])

    def test_malformed_lines_are_skipped(self):
        with self.path.open("a", encoding="utf-8") as file:
            file.write("{broken json\n")
        self.assertEqual(len(load_events(self.path)), 3)

    def test_missing_file_returns_empty_results(self):
        missing = self.path.parent / "missing.jsonl"
        self.assertEqual(load_events(missing), [])
        self.assertEqual(get_recent_events(path=missing), [])

    def test_zero_limit_returns_empty_results(self):
        self.assertEqual(get_recent_events(0, self.path), [])
        self.assertEqual(search_events("person", limit=0, path=self.path), [])


if __name__ == "__main__":
    unittest.main()
