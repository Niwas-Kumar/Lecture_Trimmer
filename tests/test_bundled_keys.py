"""The owner's Groq key travels only inside the installer, never in code updates or source."""
import tempfile
import unittest
from pathlib import Path

from lecture_trimmer.runtime import bundled_keys
from tools.release_manifest import APP_FILES


class BundledKeyTests(unittest.TestCase):
    def test_packaged_key_round_trips_and_is_not_plain_text(self):
        key = "gsk_" + "x" * 52
        encoded = bundled_keys.encode(key)
        self.assertNotIn("gsk_", encoded)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(bundled_keys.read(folder), "")  # source runs and older installers have none
            (Path(folder) / bundled_keys.FILENAME).write_text(encoded, encoding="ascii")
            self.assertEqual(bundled_keys.read(folder), key)
            (Path(folder) / bundled_keys.FILENAME).write_text("not base64!", encoding="ascii")
            self.assertEqual(bundled_keys.read(folder), "")

    def test_key_file_is_never_part_of_a_code_update_or_the_repository(self):
        self.assertFalse(any(Path(name).name == bundled_keys.FILENAME for name in APP_FILES))
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(list(root.glob(bundled_keys.FILENAME)), [])
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        for folder in ("build/", ".env"):
            self.assertIn(folder, ignored)


if __name__ == "__main__":
    unittest.main()
