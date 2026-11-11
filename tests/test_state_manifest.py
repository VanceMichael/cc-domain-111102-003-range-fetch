"""清单与本地状态（审计、隔离、恢复）的测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.manifest import load_manifest
from app.state import Entity, JobState, Segment


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_manifest(self):
        path = write_json(self.dir / "m.json", {
            "documents": [{
                "document_id": "A",
                "media_type": "application/pdf",
                "url": "https://attachment.invalid/A?t=1",
                "sha256": "a" * 64,
                "refresh_entry": "https://refresh.invalid/A",
            }]})
        entries = load_manifest(path)
        self.assertEqual(entries[0].document_id, "A")
        self.assertEqual(entries[0].expected_digest, "a" * 64)

    def test_rejects_duplicate_ids_and_bad_digest(self):
        base = {
            "media_type": "application/pdf",
            "url": "https://attachment.invalid/x",
        }
        write_json(self.dir / "dup.json", {"documents": [
            dict(base, document_id="A"), dict(base, document_id="A")]})
        with self.assertRaises(ValueError):
            load_manifest(self.dir / "dup.json")

        write_json(self.dir / "bad.json", {"documents": [
            dict(base, document_id="B", sha256="too-short")]})
        with self.assertRaises(ValueError):
            load_manifest(self.dir / "bad.json")

    def test_rejects_non_http_url_and_empty_list(self):
        write_json(self.dir / "url.json", {"documents": [{
            "document_id": "C", "media_type": "application/pdf",
            "url": "ftp://attachment.invalid/C"}]})
        with self.assertRaises(ValueError):
            load_manifest(self.dir / "url.json")
        write_json(self.dir / "empty.json", {"documents": []})
        with self.assertRaises(ValueError):
            load_manifest(self.dir / "empty.json")


class StateAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Path(self.tmp.name) / "X"
        (self.job / "segments").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _state_with_segment(self, start=0, end=7, data=b"01234567"):
        import hashlib
        name = f"seg-{start:012d}-{end:012d}"
        (self.job / "segments" / name).write_bytes(data)
        state = JobState(document_id="X", mode="range",
                         entity=Entity(etag='"e"', total_length=64,
                                       resumable=True))
        state.segments = [Segment(start, end,
                                  hashlib.sha256(data).hexdigest(), name)]
        state.save(self.job)
        return state

    def test_orphan_segment_is_quarantined(self):
        self._state_with_segment()
        # 另一个没有检查点记录的分段
        (self.job / "segments" / "seg-000000000008-000000000015").write_bytes(
            b"8-15-data")
        state = JobState.load(self.job)
        notes = state.audit(self.job)
        self.assertTrue(any("orphan" in n for n in notes))
        # 检查点背书的分段保留，没有背书的孤儿被隔离
        remaining = sorted(p.name for p in (self.job / "segments").glob("seg-*"))
        self.assertEqual(remaining, [state.segments[0].file])
        quarantine = list((self.job / "quarantine").glob("orphan-*"))
        self.assertEqual(len(quarantine), 1)

    def test_tampered_segment_is_quarantined(self):
        state = self._state_with_segment()
        target = self.job / "segments" / state.segments[0].file
        tampered = bytearray(target.read_bytes())
        tampered[2] ^= 0xFF
        target.write_bytes(bytes(tampered))

        reloaded = JobState.load(self.job)
        notes = reloaded.audit(self.job)
        self.assertTrue(any("tampered" in n for n in notes))
        self.assertEqual(reloaded.segments, [])
        self.assertTrue(list((self.job / "quarantine").glob("tampered-*")))

    def test_no_validator_segments_quarantined(self):
        state = self._state_with_segment()
        state.entity.resumable = False
        state.mode = "full_only"
        state.save(self.job)

        reloaded = JobState.load(self.job)
        notes = reloaded.audit(self.job)
        self.assertTrue(any("no-strong-validator" in n for n in notes))
        self.assertEqual(reloaded.segments, [])

    def test_failed_mode_returns_to_range_after_audit(self):
        state = self._state_with_segment()
        state.mode = "failed"
        state.save(self.job)
        # 制造一次隔离（篡改），审计后应回到可续传的 range
        target = self.job / "segments" / state.segments[0].file
        tampered = bytearray(target.read_bytes())
        tampered[0] ^= 0x01
        target.write_bytes(bytes(tampered))

        reloaded = JobState.load(self.job)
        reloaded.audit(self.job)
        self.assertEqual(reloaded.mode, "range")


class GapAndCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.state = JobState(document_id="X")

    def test_next_gap_with_missing_prefix(self):
        self.state.segments = [Segment(8, 15, "h", "s1"),
                               Segment(16, 23, "h", "s2")]
        self.assertEqual(self.state.next_gap(64), (0, 7))

    def test_next_gap_between_segments(self):
        self.state.segments = [Segment(0, 7, "h", "s1"),
                               Segment(16, 23, "h", "s2")]
        self.assertEqual(self.state.next_gap(64), (8, 15))

    def test_next_gap_at_tail(self):
        self.state.segments = [Segment(0, 63, "h", "s1")]
        self.assertIsNone(self.state.next_gap(64))

    def test_completeness_requires_full_coverage(self):
        self.state.segments = [Segment(0, 31, "h", "s1"),
                               Segment(32, 63, "h", "s2")]
        self.assertTrue(self.state.is_complete(64))
        self.state.segments = [Segment(0, 31, "h", "s1")]
        self.assertFalse(self.state.is_complete(64))
        self.state.segments = [Segment(2, 63, "h", "s1")]
        self.assertFalse(self.state.is_complete(64))


if __name__ == "__main__":
    unittest.main()
