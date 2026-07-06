"""Cross-platform parity tests (Round 9 Task A)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from harness.keys import _read_keys


class ParitySweepTests(unittest.TestCase):
    def test_keys_read_utf8_non_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            keys_path = os.path.join(tmp, "keys.json")
            with open(keys_path, "w", encoding="utf-8") as fh:
                json.dump({"demo": "caf\u00e9-key"}, fh)
            old = os.environ.get("HARNESS_STATE_DIR")
            try:
                os.environ["HARNESS_STATE_DIR"] = tmp
                self.assertEqual(_read_keys().get("demo"), "caf\u00e9-key")
            finally:
                if old is None:
                    os.environ.pop("HARNESS_STATE_DIR", None)
                else:
                    os.environ["HARNESS_STATE_DIR"] = old
