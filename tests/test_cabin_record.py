"""Closed cabin records. No camera and no model."""

from __future__ import annotations

import unittest

from cms.cabin_record import parse_record


class ParseRecordTest(unittest.TestCase):
    def test_one_object_is_kept(self) -> None:
        text = (
            '{"event":"phone","confidence":0.99,"subjects":["passenger"],'
            '"description":"holding a phone","severity":"low"}'
        )
        record = parse_record(text)
        self.assertEqual(record["event"], "phone")
        self.assertEqual(record["confidence"], 0.99)
        self.assertEqual(record["subjects"], ["passenger"])

    def test_a_list_keeps_the_strongest_event(self) -> None:
        text = """```json
[
	{"event": "phone", "confidence": 0.99},
	{"event": "reaching", "confidence": 0.95},
	{"event": "lying_down", "confidence": 0.90},
	{"event": "altercation", "confidence": 0.85},
	{"event": "unusual_object", "confidence": 0.92}
]"""
        record = parse_record(text)
        self.assertEqual(record["event"], "phone")
        self.assertEqual(record["confidence"], 0.99)

    def test_unknown_text_becomes_none(self) -> None:
        record = parse_record("a person is sitting")
        self.assertEqual(record["event"], "none")
        self.assertIsNone(record["confidence"])


if __name__ == "__main__":
    unittest.main()
