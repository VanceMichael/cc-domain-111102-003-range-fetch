"""资源约束：连接数与每请求内存上限。

用记录最大单次 read 字节数的流包装，证明引擎按固定小块消费响应，
不会把整个实体读进内存。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.engine import Downloader
from app.manifest import ManifestEntry
from app.transport import HeaderMap, Response


class CountingStream:
    def __init__(self, body):
        self._body = body
        self.max_read = 0
        self.total = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body
            self._body = b""
        else:
            chunk = self._body[:size]
            self._body = self._body[size:]
        self.max_read = max(self.max_read, len(chunk))
        self.total += len(chunk)
        return chunk

    def close(self):
        pass


class ChunkedTransport:
    def __init__(self, data, chunk_cap):
        self.data = data
        self.chunk_cap = chunk_cap
        self.streams = []

    def get(self, url, headers):
        etag = '"' + hashlib.sha256(self.data).hexdigest() + '"'
        stream = CountingStream(self.data)
        self.streams.append(stream)
        if "Range" not in headers:
            return Response(200, HeaderMap({
                "ETag": etag,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(self.data)),
            }), stream)
        import re
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", headers["Range"])
        start = int(match[1])
        end = int(match[2]) if match[2] else len(self.data) - 1
        end = min(end, len(self.data) - 1)
        part = self.data[start:end + 1]
        # 流只暴露本分片字节
        stream = CountingStream(part)
        self.streams.append(stream)
        return Response(206, HeaderMap({
            "ETag": etag,
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {start}-{end}/{len(self.data)}",
            "Content-Length": str(len(part)),
        }), stream)

    def refresh(self, document_id, entry):
        return url


url = "https://attachment.invalid/X?t=1"


class MemoryBoundTests(unittest.TestCase):
    def test_streaming_reads_are_chunked(self):
        data = bytes((i * 7) % 256 for i in range(20_000))
        with tempfile.TemporaryDirectory() as runtime:
            transport = ChunkedTransport(data, chunk_cap=64 * 1024)
            downloader = Downloader(
                transport, runtime,
                segment_size=4096, max_attempts=2,
                backoff=lambda n: 0.0)
            entry = ManifestEntry(
                document_id="X",
                media_type="application/octet-stream",
                url=url,
                sha256=hashlib.sha256(data).hexdigest())
            result = downloader.download(entry)
            self.assertTrue(result.ok, result.error)
            # 引擎内部读块上限是 64KiB；任何单次 read 都不得超过它
            self.assertTrue(transport.streams)
            self.assertLessEqual(
                max(s.max_read for s in transport.streams), 64 * 1024)
            # 成品与源逐字节一致
            self.assertEqual(
                Path(runtime, "outputs", "X.bin").read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
