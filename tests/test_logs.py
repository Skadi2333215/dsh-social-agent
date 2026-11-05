import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import zstandard

from social_agent.logs import LogExporter
from tests.helpers import write_log


class LogExporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # 临时目录可能使用 Windows 短路径或 macOS 别名，与生产代码统一为真实路径。
        self.root = Path(self.temporary.name).resolve()
        self.home, self.cwd = self.root / "dsh", self.root / "中文工作目录"
        self.cwd.mkdir()
        self.exporter = LogExporter(self.home)

    def export(self, session_id="session-test-001"):
        return self.exporter.export(session_id, cwd=self.cwd, output_dir=self.root / "export")

    def test_preserves_all_frames_unknown_events_and_large_chinese_content(self):
        events = [
            {"type": "assistant/message", "data": {"text": "中文" * 30_000}},
            {"type": "future/custom-event", "unknown": {"keep": [1, 2, 3]}},
        ]
        source, expected = write_log(self.home, self.cwd, events=events)
        result = self.export()
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.jsonl_path.read_bytes(), expected)
        self.assertEqual(result.raw_path.read_bytes(), source.read_bytes())
        self.assertEqual(result.sha256, hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(result.event_count, 3)

    def test_selects_numeric_highest_generation_and_ignores_noncanonical_names(self):
        older, _ = write_log(self.home, self.cwd, version=9)
        latest, expected = write_log(self.home, self.cwd, version=10)
        (older.parent / "session.v999.jsonl.zstd.tmp").write_bytes(b"invalid")
        (older.parent / "session.v010.jsonl.zstd").write_bytes(b"invalid")
        result = self.export()
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.source_path, latest)
        self.assertEqual(result.jsonl_path.read_bytes(), expected)

    def test_supports_legacy_version_zero(self):
        write_log(self.home, self.cwd, version=0)
        result = self.export()
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(result.format_version, 0)

    def test_wrong_cwd_preserves_raw_but_does_not_publish_jsonl(self):
        source, _ = write_log(self.home, self.root / "other")
        result = self.export()
        self.assertFalse(result.succeeded)
        self.assertIn("工作目录", result.error)
        self.assertEqual(result.raw_path.read_bytes(), source.read_bytes())
        self.assertTrue(result.partial_path.exists())
        self.assertFalse((self.root / "export" / "session.jsonl").exists())

    def test_header_session_id_is_verified(self):
        source, _ = write_log(self.home, self.cwd)
        header = {"type": "session", "id": "session-other", "cwd": str(self.cwd), "version": 4}
        source.write_bytes(zstandard.ZstdCompressor().compress((json.dumps(header) + "\n").encode()))
        self.assertIn("会话编号", self.export().error)

    def test_truncated_frame_is_reported_even_after_valid_frames(self):
        source, _ = write_log(self.home, self.cwd, events=[{"type": "user/message", "text": "不能丢失"}])
        original = source.read_bytes()[:-2]
        source.write_bytes(original)
        result = self.export()
        self.assertFalse(result.succeeded)
        self.assertIn("截断", result.error)
        self.assertEqual(result.raw_path.read_bytes(), original)
        self.assertIsNotNone(result.partial_path)

    def test_bad_checksum_is_reported(self):
        source, _ = write_log(self.home, self.cwd)
        content = bytearray(source.read_bytes())
        content[-1] ^= 0xFF
        source.write_bytes(content)
        self.assertFalse(self.export().succeeded)

    def test_invalid_json_line_is_not_silently_skipped(self):
        source, _ = write_log(self.home, self.cwd)
        with source.open("ab") as handle:
            handle.write(zstandard.ZstdCompressor().compress(b'{"type":\n'))
        result = self.export()
        self.assertFalse(result.succeeded)
        self.assertIn("第 2 行", result.error)

    def test_missing_and_ambiguous_sessions_are_rejected(self):
        self.assertIn("0 个", self.export().error)
        source, _ = write_log(self.home, self.cwd)
        other = self.home / "sessions" / "--other--" / source.parent.name
        other.mkdir(parents=True)
        (other / source.name).write_bytes(source.read_bytes())
        self.assertIn("2 个", self.export().error)

    def test_path_traversal_and_existing_output_are_rejected(self):
        self.assertIn("安全", self.export("../../outside").error)
        write_log(self.home, self.cwd)
        first = self.export()
        before = first.raw_path.read_bytes()
        second = self.export()
        self.assertFalse(second.succeeded)
        self.assertEqual(first.raw_path.read_bytes(), before)
