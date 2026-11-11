"""传输适配与资源约束测试。"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from app.transport import (
    HeaderMap,
    RefreshError,
    ScriptedTransport,
    _MemoryStream,
)


class HeaderMapTests(unittest.TestCase):
    def test_case_insensitive(self):
        headers = HeaderMap({"Content-Type": "application/pdf"})
        self.assertEqual(headers.get("content-type"), "application/pdf")
        self.assertEqual(headers.get("CONTENT-TYPE"), "application/pdf")
        self.assertIsNone(headers.get("missing"))
        self.assertIn("content-type", headers)


class MemoryStreamTests(unittest.TestCase):
    def test_chunked_read_and_close(self):
        stream = _MemoryStream(b"abcdef")
        with stream:
            self.assertEqual(stream.read(2), b"ab")
            self.assertEqual(stream.read(), b"cdef")
            self.assertEqual(stream.read(4), b"")
        with self.assertRaises(ValueError):
            stream.read(1)


class ScriptedTransportTests(unittest.TestCase):
    def test_behavior_sequence_then_recovers(self):
        transport = ScriptedTransport(
            "fixtures",
            behaviors={"A": [
                {"behavior": "short", "times": 2, "then": "normal"}
            ]})
        url = "https://attachment.invalid/A?t=1"
        bodies = []
        for _ in range(4):
            response = transport.get(url, {"Range": "bytes=0-15"})
            self.assertEqual(response.status, 206)
            with response as stream:
                bodies.append(stream.read())
        # 前两次 short：声明 0-15 共 16 字节只给 15；恢复后给满 16
        self.assertEqual([len(body) for body in bodies], [15, 15, 16, 16])

    def test_refresh_failure_is_specific_to_document(self):
        transport = ScriptedTransport(
            "fixtures", refresh_fail_ids={"B"})
        self.assertTrue(transport.refresh("A", "entry").endswith("renewed"))
        with self.assertRaises(RefreshError):
            transport.refresh("B", "entry")

    def test_login_behavior_returns_html(self):
        transport = ScriptedTransport(
            "fixtures", behaviors={"A": "login"})
        response = transport.get(
            "https://attachment.invalid/A?t=1", {"Range": "bytes=0-7"})
        self.assertEqual(response.status, 200)
        self.assertIn("html", response.headers.get("Content-Type"))


class ReplayRouteTests(unittest.TestCase):
    def test_base64_body_roundtrip(self):
        from app.wire import Replay
        payload = base64.b64encode(b"\x00\x01binary").decode()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "routes.json"
            path.write_text(json.dumps(
                {"https://x.invalid/f": [
                    {"status": 206, "headers": {}, "body": payload,
                     "base64": True}]}))
            reply = Replay(path).get("https://x.invalid/f")
            self.assertEqual(reply.body, b"\x00\x01binary")


if __name__ == "__main__":
    unittest.main()
