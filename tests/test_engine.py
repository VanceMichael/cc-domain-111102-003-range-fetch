"""引擎故障语义测试：每种异常响应都不能污染已验证字节。"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.engine import Downloader, FatalDownload, Hooks
from app.manifest import ManifestEntry
from app.state import JobState
from app.transport import HeaderMap, RefreshError, Response
from app.transport import _MemoryStream


def etag_of(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest() + '"'


class FakeTransport:
    """可精确编排响应序列的内存传输。"""

    def __init__(self, data: bytes, media_type: str = "application/octet-stream",
                 script=None):
        self.data = data
        self.media_type = media_type
        # script: 每次 get 依次取出的函数(data, headers) -> Reply-like dict
        self.script = list(script or [])
        self.requests = []
        self.urls = []
        self.refresh_raises = False

    def get(self, url, headers):
        self.requests.append(dict(headers))
        self.urls.append(url)
        if self.script:
            producer = self.script.pop(0)
            return producer(self.data, dict(headers))
        return self._normal(dict(headers))

    def _normal(self, headers):
        data = self.data
        range_header = headers.get("Range")
        etag = etag_of(data)
        base = {"ETag": etag, "Content-Type": self.media_type,
                "Content-Length": str(len(data))}
        if not range_header:
            return Response(200, HeaderMap(base), _MemoryStream(data))
        import re
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
        start = int(match[1])
        end = int(match[2]) if match[2] else len(data) - 1
        end = min(end, len(data) - 1)
        if start >= len(data):
            return Response(416, HeaderMap(
                {"Content-Range": f"bytes */{len(data)}"}), _MemoryStream(b""))
        part = data[start:end + 1]
        merged = dict(base)
        merged.update({
            "Content-Range": f"bytes {start}-{end}/{len(data)}",
            "Content-Length": str(len(part)),
        })
        return Response(206, HeaderMap(merged), _MemoryStream(part))

    def refresh(self, document_id, entry):
        if self.refresh_raises:
            raise RefreshError("刷新入口 503")
        return url_of(document_id, renewed=True)


def url_of(document_id, renewed=False):
    suffix = "renewed" if renewed else "1"
    return f"https://attachment.invalid/{document_id}?temporary={suffix}"


def make_entry(document_id="X", data=b"", sha256=None,
               media_type="application/octet-stream"):
    return ManifestEntry(
        document_id=document_id,
        media_type=media_type,
        url=url_of(document_id),
        refresh_entry=f"https://refresh.invalid/{document_id}",
        sha256=sha256 or hashlib.sha256(data).hexdigest(),
    )


def reply(status, headers, body=b""):
    return Response(status, HeaderMap(headers), _MemoryStream(body))


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def downloader(self, transport, **kwargs):
        options = dict(segment_size=8, max_attempts=4,
                       backoff=lambda n: 0.0)
        options.update(kwargs)
        return Downloader(transport, self.runtime, **options)

    def state(self, document_id="X"):
        return JobState.load(self.runtime / "state" / document_id)


class HappyPathTests(EngineTestBase):
    def test_segmented_download_matches_source(self):
        data = bytes(range(256))
        transport = FakeTransport(data)
        result = self.downloader(transport).download(make_entry(data=data))
        self.assertTrue(result.ok, result.error)
        output = Path(result.output_path).read_bytes()
        self.assertEqual(output, data)
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        # 首个请求尚无 ETag 可带；从第二个分段起必须带 If-Range
        self.assertGreater(len(transport.requests), 1)
        self.assertTrue(all("Range" in r for r in transport.requests))
        self.assertNotIn("If-Range", transport.requests[0])
        self.assertTrue(all("If-Range" in r for r in transport.requests[1:]))


class ServerIgnoreRangeTests(EngineTestBase):
    def test_200_instead_of_206_restarts_and_publishes(self):
        data = bytes(range(64))

        def ignore_once(payload, headers):
            return reply(200, {
                "ETag": etag_of(payload),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
            }, payload)

        transport = FakeTransport(data, script=[ignore_once])
        result = self.downloader(transport).download(make_entry(data=data))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(Path(result.output_path).read_bytes(), data)
        # 旧请求带了 Range，被服务端忽略后没有拼接到任何分段
        quarantine = list((self.runtime / "state" / "X" / "quarantine").glob(
            "server-ignored-range-*"))
        # 首个请求前没有分段可隔离，因此这里只验证成品正确
        self.assertEqual(quarantine, [])

    def test_200_after_partial_progress_quarantines_old_segments(self):
        data = bytes(range(64))
        transport = FakeTransport(data)
        entry = make_entry(data=data)
        d = self.downloader(transport)

        class KillAfterFirst(Hooks):
            def after_checkpoint_saved(self, document_id, state):
                if len(state.segments) >= 1:
                    raise SystemExit("模拟中断")

        d.hooks = KillAfterFirst()
        with self.assertRaises(SystemExit):
            d.download(entry)
        self.assertEqual(len(self.state().segments), 1)

        # 新进程：服务端开始忽略 Range，始终回 200
        def always_ignore(payload, headers):
            return reply(200, {
                "ETag": etag_of(payload),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
            }, payload)

        transport2 = FakeTransport(data, script=[always_ignore])
        result = self.downloader(transport2).download(entry)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(Path(result.output_path).read_bytes(), data)
        quarantine = list((self.runtime / "state" / "X" / "quarantine").glob(
            "server-ignored-range-*"))
        self.assertTrue(quarantine, "旧分段必须被隔离而不是拼接")


class LoginPageTests(EngineTestBase):
    @staticmethod
    def _login_page(payload, headers):
        return reply(200, {"Content-Type": "text/html"},
                     b"<!doctype html><html>login</html>")

    def test_html_200_triggers_refresh_then_succeeds(self):
        data = bytes(range(64))
        # 首次返回登录页；刷新换地址后，后续请求走正常序列
        transport = FakeTransport(data, script=[self._login_page])
        result = self.downloader(transport).download(make_entry(data=data))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.refreshes, 1)
        self.assertEqual(Path(result.output_path).read_bytes(), data)
        # 登录页字节没有进入任何分段
        self.assertTrue(all(
            b"login" not in p.read_bytes()
            for p in (self.runtime / "state" / "X" / "segments").glob("seg-*")))

    def test_persistent_login_page_fails_without_output(self):
        data = bytes(range(64))
        # 登录页始终出现，刷新次数耗尽后必须失败且无成品
        transport = FakeTransport(
            data, script=[self._login_page] * 10)
        result = self.downloader(transport, max_refreshes=2).download(
            make_entry(data=data))
        self.assertFalse(result.ok)
        self.assertIn("刷新次数已达上限", result.error)
        self.assertFalse(Path(self.runtime, "outputs", "X.bin").exists())
        # 没有任何分段被记录
        self.assertEqual(self.state().segments, [])


class ShortResponseTests(EngineTestBase):
    def test_short_206_then_retry_succeeds(self):
        data = bytes(range(64))

        def short_then_normal(transport_self):
            calls = {"n": 0}

            def producer(payload, headers):
                calls["n"] += 1
                if calls["n"] == 1:
                    part = payload[:7]  # 声明 8 字节只给 7
                    return reply(206, {
                        "ETag": etag_of(payload),
                        "Content-Type": "application/octet-stream",
                        "Content-Range": "bytes 0-7/64",
                        "Content-Length": "8",
                    }, part)
                return FakeTransport._normal(transport_self, headers)
            return producer

        transport = FakeTransport(data)
        transport.script = [short_then_normal(transport)]
        result = self.downloader(transport).download(make_entry(data=data))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(Path(result.output_path).read_bytes(), data)
        # 短响应的临时文件不得残留
        leftovers = list((self.runtime / "state" / "X" / "segments").glob(
            "*.tmp"))
        self.assertEqual(leftovers, [])


class WrongContentRangeTests(EngineTestBase):
    def _partial(self, data, entry, segments=1):
        transport = FakeTransport(data)
        d = self.downloader(transport)

        class KillAfterFirst(Hooks):
            def after_checkpoint_saved(self, document_id, state):
                if len(state.segments) == segments:
                    raise SystemExit("中断")

        d.hooks = KillAfterFirst()
        with self.assertRaises(SystemExit):
            d.download(entry)

    def test_wrong_start_is_rejected(self):
        data = bytes(range(64))
        entry = make_entry(data=data)
        self._partial(data, entry)  # 已验证 0-7，下个请求是 8-15

        def wrong_start(payload, headers):
            # 请求 8-15，响应却宣称从 0 开始
            return reply(206, {
                "ETag": etag_of(payload),
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-7/64",
                "Content-Length": "8",
            }, payload[0:8])

        transport = FakeTransport(data, script=[wrong_start])
        result = self.downloader(transport).download(entry)
        self.assertFalse(result.ok)
        self.assertIn("Content-Range 起点", result.error)

    def test_overlapping_segment_guard(self):
        """重叠防护是纵深防御：正常协议下起点检查会先触发，
        这里直接构造通过起点检查、但与已验证分段重叠的状态。"""
        data = bytes(range(64))
        entry = make_entry(data=data)
        self._partial(data, entry)  # 已验证 0-7
        d = self.downloader(FakeTransport(data))
        state = self.state()
        # 请求 8-15，响应 Content-Range 起点正确，但已验证分段 0-7
        # 被人为扩展为 0-15，制造真实重叠。
        from app.state import Segment
        state.segments = [Segment(0, 15, state.segments[0].sha256,
                                  state.segments[0].file)]
        response = reply(206, {
            "ETag": etag_of(data),
            "Content-Type": "application/octet-stream",
            "Content-Range": "bytes 8-15/64",
            "Content-Length": "8",
        }, data[8:16])
        with self.assertRaises(FatalDownload) as ctx:
            d._handle_response(entry, state,
                               d.job_dir("X"), response,
                               requested=(8, 15), url=entry.url)
        self.assertIn("重叠", str(ctx.exception))
        # 重叠内容没有落盘，原分段记录保持不变
        self.assertEqual([(s.start, s.end) for s in state.segments], [(0, 15)])


class EtagChangeTests(EngineTestBase):
    def test_etag_change_on_206_is_fatal(self):
        data = bytes(range(64))
        transport = FakeTransport(data)
        entry = make_entry(data=data)
        d = self.downloader(transport)

        class KillAfterFirst(Hooks):
            def after_checkpoint_saved(self, document_id, state):
                if len(state.segments) == 1:
                    raise SystemExit("中断")

        d.hooks = KillAfterFirst()
        with self.assertRaises(SystemExit):
            d.download(entry)

        def changed_etag(payload, headers):
            return reply(206, {
                "ETag": '"' + "0" * 64 + '"',
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 8-15/64",
                "Content-Length": "8",
            }, payload[8:16])

        transport2 = FakeTransport(data, script=[changed_etag])
        result = self.downloader(transport2).download(entry)
        self.assertFalse(result.ok)
        self.assertIn("ETag 已变化", result.error)
        self.assertFalse(Path(self.runtime, "outputs", "X.bin").exists())


class FourOneSixTests(EngineTestBase):
    def test_416_when_incomplete_is_retried(self):
        data = bytes(range(64))
        transport = FakeTransport(data)
        entry = make_entry(data=data)
        d = self.downloader(transport)

        class KillAfterFirst(Hooks):
            def after_checkpoint_saved(self, document_id, state):
                if len(state.segments) == 1:
                    raise SystemExit("中断")

        d.hooks = KillAfterFirst()
        with self.assertRaises(SystemExit):
            d.download(entry)

        # 第一次续传先回 416，之后恢复正常
        def four_one_six(payload, headers):
            return reply(416, {"Content-Range": "bytes */64"}, b"")

        transport2 = FakeTransport(data, script=[four_one_six])
        result = self.downloader(transport2).download(entry)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(Path(result.output_path).read_bytes(), data)


class ExpiredRefreshTests(EngineTestBase):
    def test_expired_url_refreshes_and_continues(self):
        data = bytes(range(64))

        def expired_once(payload, headers):
            return reply(403, {}, b"expired")

        transport = FakeTransport(data, script=[expired_once])
        result = self.downloader(transport).download(make_entry(data=data))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.refreshes, 1)
        # 首次请求用旧地址失败，刷新后的请求走新 URL
        self.assertIn("temporary=1", transport.urls[0])
        self.assertTrue(any("renewed" in u for u in transport.urls))

    def test_refresh_failure_blocks_only_this_document(self):
        data_a = bytes(range(32))
        data_b = bytes(range(32, 96))
        transport_a = FakeTransport(data_a)
        transport_b = FakeTransport(data_b)
        transport_b.refresh_raises = True

        def expired(payload, headers):
            return reply(403, {}, b"expired")

        transport_b.script = [expired]
        # 两个下载器共用 runtime，但各自传输
        da = Downloader(transport_a, self.runtime, segment_size=8,
                        backoff=lambda n: 0.0)
        db = Downloader(transport_b, self.runtime, segment_size=8,
                        max_attempts=2, backoff=lambda n: 0.0)
        result_a = da.download(make_entry("A", data_a))
        result_b = db.download(make_entry("B", data_b))
        self.assertTrue(result_a.ok)
        self.assertFalse(result_b.ok)
        self.assertIn("刷新", result_b.error)
        self.assertTrue(Path(self.runtime, "outputs", "A.bin").exists())
        self.assertFalse(Path(self.runtime, "outputs", "B.bin").exists())


class NoStrongValidatorTests(EngineTestBase):
    def test_full_only_mode_segments_are_quarantined_on_restart(self):
        data = bytes(range(64))

        def no_etag_full(transport_self):
            def producer(payload, headers):
                return reply(200, {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(payload)),
                }, payload)
            return producer

        transport = FakeTransport(data, script=[no_etag_full(None)])
        entry = make_entry(data=data)
        d = self.downloader(transport)

        class KillMidFull(Hooks):
            def after_checkpoint_saved(self, document_id, state):
                if len(state.segments) == 2:
                    raise SystemExit("中断")

        d.hooks = KillMidFull()
        with self.assertRaises(SystemExit):
            d.download(entry)
        self.assertEqual(self.state().mode, "full_only")

        # 新进程：审计必须隔离无强校验器的分段
        transport2 = FakeTransport(data, script=[no_etag_full(None)])
        result = self.downloader(transport2).download(entry)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.mode, "full_only")
        quarantine = list((self.runtime / "state" / "X" / "quarantine").glob(
            "no-strong-validator-*"))
        self.assertTrue(quarantine)
        self.assertEqual(Path(result.output_path).read_bytes(), data)


class ShaMismatchTests(EngineTestBase):
    def test_wrong_expected_digest_blocks_publish(self):
        data = bytes(range(64))
        entry = make_entry(data=data, sha256="a" * 64)
        transport = FakeTransport(data)
        result = self.downloader(transport).download(entry)
        self.assertFalse(result.ok)
        self.assertIn("SHA-256 不匹配", result.error)
        self.assertFalse(Path(self.runtime, "outputs", "X.bin").exists())
        # 暂存文件已清理
        self.assertEqual(list(Path(self.runtime, "outputs").glob(".*.staging")),
                         [])


class ResumeAndTamperTests(EngineTestBase):
    def _partial_download(self, data, entry, at_segments):
        transport = FakeTransport(data)
        d = self.downloader(transport)

        class Hook(Hooks):
            def after_checkpoint_saved(self, document_id, state):
                if len(state.segments) == at_segments:
                    raise SystemExit("中断")

        d.hooks = Hook()
        with self.assertRaises(SystemExit):
            d.download(entry)

    def test_resume_in_new_process_completes(self):
        data = bytes(range(64))
        entry = make_entry(data=data)
        self._partial_download(data, entry, 2)
        transport = FakeTransport(data)
        result = self.downloader(transport).download(entry)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(Path(result.output_path).read_bytes(), data)
        # 续传请求携带 If-Range
        self.assertTrue(any("If-Range" in r for r in transport.requests))

    def test_tampered_segment_is_quarantined_and_redownloaded(self):
        data = bytes(range(64))
        entry = make_entry(data=data)
        self._partial_download(data, entry, 2)
        target = sorted((self.runtime / "state" / "X" / "segments").glob(
            "seg-*"))[0]
        tampered = bytearray(target.read_bytes())
        tampered[3] ^= 0x5A
        target.write_bytes(bytes(tampered))

        transport = FakeTransport(data)
        result = self.downloader(transport).download(entry)
        self.assertTrue(result.ok, result.error)
        quarantine = list((self.runtime / "state" / "X" / "quarantine").glob(
            "tampered-*"))
        self.assertTrue(quarantine)
        self.assertEqual(Path(result.output_path).read_bytes(), data)


class ConnectionLimitTests(EngineTestBase):
    def test_max_connections_is_respected(self):
        payloads = {"D1": bytes(range(16)),
                    "D2": bytes(range(16, 32)),
                    "D3": bytes(range(32, 48))}
        entries = [make_entry(doc, payload) for doc, payload in payloads.items()]
        transports = {doc: FakeTransport(payload)
                      for doc, payload in payloads.items()}

        class RoutingTransport:
            def __init__(self):
                self.concurrent = 0
                self.peak = 0

            def get(self, url, headers):
                self.concurrent += 1
                self.peak = max(self.peak, self.concurrent)
                doc = url.split("/")[-1].split("?")[0]
                try:
                    return transports[doc].get(url, headers)
                finally:
                    self.concurrent -= 1

            def refresh(self, document_id, entry):
                return transports[document_id].refresh(document_id, entry)

        routing = RoutingTransport()
        d = Downloader(routing, self.runtime,
                       max_connections=1, segment_size=8,
                       backoff=lambda n: 0.0)
        results = d.run_entries(entries)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual({r.document_id for r in results},
                         {"D1", "D2", "D3"})
        # 三个文档、连接上限为 1：观测到的并发峰值不得超过 1
        self.assertEqual(routing.peak, 1)
        # 每个成品字节正确
        for doc, payload in payloads.items():
            self.assertEqual(
                Path(self.runtime, "outputs", f"{doc}.bin").read_bytes(),
                payload)


if __name__ == "__main__":
    unittest.main()
