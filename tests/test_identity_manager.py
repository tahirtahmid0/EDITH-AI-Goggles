"""Offline unit tests for EDITH's short-term visual identity manager.

Run from the repository root:
    python -m unittest discover -s tests -v
"""

import unittest

from brain.identity_manager import IdentityManager


class IdentityManagerTests(unittest.TestCase):
    FRAME_SIZE = (1000, 1000)

    def setUp(self):
        self.manager = IdentityManager()

    @staticmethod
    def observation(track_id, class_name="person", bbox=(100, 100, 200, 200)):
        x1, y1, x2, y2 = bbox
        return {
            "track_id": track_id,
            "class_name": class_name,
            "bbox": bbox,
            "center": ((x1 + x2) / 2, (y1 + y2) / 2),
        }

    def assign(self, track_id, now, class_name="person", bbox=(100, 100, 200, 200)):
        return self.manager.assign(
            self.observation(track_id, class_name, bbox),
            self.FRAME_SIZE,
            now,
        )

    def test_first_detection_creates_identity(self):
        result = self.assign(1, 1.0)

        self.assertEqual(result["identity_match"], "new")
        self.assertEqual(result["visual_identity_id"], "V0001")
        self.assertTrue(result["identity_created"])

    def test_same_track_keeps_identity_across_frames(self):
        first = self.assign(1, 1.0)
        self.manager.begin_frame()
        second = self.assign(1, 1.4, bbox=(110, 100, 210, 200))

        self.assertEqual(second["visual_identity_id"], first["visual_identity_id"])
        self.assertEqual(second["identity_match"], "same_track")
        self.assertFalse(second["identity_created"])

    def test_new_track_reconnects_to_recent_matching_identity(self):
        first = self.assign(1, 1.0)
        self.manager.begin_frame()
        returned = self.assign(2, 2.0, bbox=(105, 100, 205, 200))

        self.assertEqual(returned["visual_identity_id"], first["visual_identity_id"])
        self.assertEqual(returned["identity_match"], "reconnected")
        self.assertEqual(returned["previous_track_id"], 1)
        self.assertEqual(self.manager.identity_for_track(2), first["visual_identity_id"])

    def test_different_class_does_not_reuse_identity(self):
        person = self.assign(1, 1.0)
        self.manager.begin_frame()
        bottle = self.assign(2, 1.2, class_name="bottle")

        self.assertNotEqual(bottle["visual_identity_id"], person["visual_identity_id"])
        self.assertEqual(bottle["identity_match"], "new")

    def test_expired_identity_is_not_reused(self):
        first = self.assign(1, 1.0)
        self.manager.begin_frame()
        returned = self.assign(2, 8.0)

        self.assertNotEqual(returned["visual_identity_id"], first["visual_identity_id"])
        self.assertEqual(returned["identity_match"], "new")

    def test_ambiguous_match_is_reported_without_forcing_reconnection(self):
        first = self.assign(1, 1.0)
        self.manager.begin_frame()
        # Same-sized box, no overlap, and a plausible but uncertain displacement.
        possible = self.assign(2, 1.5, bbox=(270, 100, 370, 200))

        self.assertEqual(possible["identity_match"], "uncertain_new")
        self.assertEqual(possible["possible_match_to"], first["visual_identity_id"])
        self.assertNotEqual(possible["visual_identity_id"], first["visual_identity_id"])

    def test_one_identity_cannot_be_assigned_twice_in_one_frame(self):
        first = self.assign(1, 1.0)
        # Deliberately do not call begin_frame: this simulates a duplicate assignment
        # during the same processed frame.
        duplicate = self.assign(2, 1.01)

        self.assertNotEqual(duplicate["visual_identity_id"], first["visual_identity_id"])

    def test_identity_ids_increment(self):
        first = self.assign(1, 1.0)
        self.manager.begin_frame()
        second = self.assign(2, 1.2, class_name="bottle")

        self.assertEqual(first["visual_identity_id"], "V0001")
        self.assertEqual(second["visual_identity_id"], "V0002")


if __name__ == "__main__":
    unittest.main()
