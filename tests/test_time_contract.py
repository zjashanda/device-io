"""The workspace time contract, enforced as tests.

Source of truth: ``device_io.schema.TIME_CONTRACT``. Every timestamp this tool
writes is Beijing wall-clock time in ``YYYY-MM-DD HH:MM:SS``; an unmodified
external timestamp may only travel in a ``raw_*`` field.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from device_io import schema  # noqa: E402
from device_io.backends.windows import normalize_raw  # noqa: E402

ALLOWED = schema.TIME_PATTERN


class FormatTest(unittest.TestCase):
    def test_now_bj_matches_contract(self):
        value = schema.now_bj()
        self.assertRegex(value, ALLOWED)
        self.assertEqual(len(value), 19)
        self.assertNotIn("T", value)
        self.assertTrue(schema.is_bj_timestamp(value))

    def test_contract_metadata(self):
        self.assertEqual(schema.TIME_CONTRACT["format"], "YYYY-MM-DD HH:MM:SS")
        self.assertEqual(schema.TIME_CONTRACT["offset_in_string"], "none")
        self.assertEqual(schema.TIME_CONTRACT["zone"], "Asia/Shanghai")
        self.assertRegex(schema.TIME_CONTRACT["example"], ALLOWED)

    def test_rejects_old_shape_and_rfc3339(self):
        rejected = [
            "2026-11-10T20:00:00.000",  # time-lint: allow
            "2026-11-10T20:00:00",  # time-lint: allow
            "2026-11-10 20:00:00Z",  # time-lint: allow
            "2026-11-10 20:00:00+08:00",  # time-lint: allow
            "2026-11-10 20:00:00.500",  # time-lint: allow
            "2026-11-10",
            "",
            None,
        ]
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(schema.is_bj_timestamp(value))
                with self.assertRaises(schema.SchemaError):
                    schema.require_bj_timestamp(value, "generated_at")

    def test_accepts_contract_shape(self):
        self.assertEqual(
            schema.require_bj_timestamp("2026-11-10 20:00:00", "generated_at"),
            "2026-11-10 20:00:00",
        )


class SchemaPatternTest(unittest.TestCase):
    def test_every_timestamp_pattern_is_the_contract(self):
        seen = 0

        def walk(node, trail):
            nonlocal seen
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "pattern" and isinstance(value, str) and "\\d{4}" in value:
                        seen += 1
                        self.assertEqual(value, schema.TIME_PATTERN, trail)
                    else:
                        walk(value, "{0}.{1}".format(trail, key))
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, "{0}[{1}]".format(trail, index))

        for name, document in schema.all_schemas().items():
            walk(document, name)
        self.assertGreaterEqual(seen, 5)


class FixtureTest(unittest.TestCase):
    def test_snapshots_validate(self):
        paths = sorted((ROOT / "fixtures").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(fixture=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(document["schema"], schema.SCHEMAS["snapshot"])
                self.assertRegex(document["generated_at"], ALLOWED)
                snapshot = schema_roundtrip(document)
                self.assertEqual(snapshot["generated_at"], document["generated_at"])

    def test_raw_collector_fixtures_validate(self):
        paths = sorted((ROOT / "tests" / "fixtures").glob("*_raw.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(fixture=path.name):
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertRegex(document["generated_at"], ALLOWED)
                result = normalize_raw(document)
                self.assertIsInstance(result.warnings, list)


class RawExceptionTest(unittest.TestCase):
    def test_raw_fields_survive_verbatim(self):
        path = ROOT / "tests" / "fixtures" / "windows_ch344_raw.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        result = normalize_raw(document)
        port = next(item for item in result.ports if item.device == "COM13")
        self.assertEqual(port.extra["raw_last_seen"], "2026-11-10T12:11:03Z")  # time-lint: allow

    def test_raw_fields_helper_is_selective(self):
        picked = schema.raw_fields(
            {"generated_at": "2026-11-10 20:00:00", "raw_seen": "anything", "name": "COM1"}
        )
        self.assertEqual(picked, {"raw_seen": "anything"})
        self.assertTrue(schema.is_raw_field("RAW_SEEN"))
        self.assertFalse(schema.is_raw_field("generated_at"))


def schema_roundtrip(document):
    from device_io.models import Snapshot

    snapshot = Snapshot.from_dict(document)
    return snapshot.to_dict(include_boards=False)


if __name__ == "__main__":
    unittest.main()
