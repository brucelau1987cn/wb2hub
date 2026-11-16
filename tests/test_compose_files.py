"""Keep the two standalone Compose entry points identical; no runtime imports."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ComposeFilesTest(unittest.TestCase):
    def test_explicit_docker_compose_entry_matches_existing_default(self):
        entry = ROOT / 'docker-compose.yml'
        self.assertTrue(entry.is_file(), 'docker-compose.yml must be available as a standalone entry')
        self.assertEqual(entry.read_bytes(), (ROOT / 'compose.yml').read_bytes(),
                         'Update both Compose entry points together')


if __name__ == '__main__':
    unittest.main()
