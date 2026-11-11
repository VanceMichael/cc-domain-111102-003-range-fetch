"""分段下载引擎：Range/If-Range 续传、故障处理与原子发布。

设计决策（无稳定校验器的保守方案）
-----------------------------------
HTTP 允许把 Last-Modified 放进 If-Range，但它只精确到秒，弱 ETag（W/）
明确不支持 Range 语义。因此本引擎**只承认强 ETag**：

* 拿到过强 ETag 的实体允许跨进程按分段续传，请求一律带 ``If-Range``；
* 源始终不给强 ETag 时，进入 ``full_only`` 模式：单进程内仍可按分段
  组织字节（每段都带独立 SHA-256，短响应可立即发现），但进程退出后
  这些分段在下次启动时整体隔离并全量重下。代价是每次中断都要重新
  下载整个文件，换来的保证是绝不把来源不明的旧字节拼进新实体。

任何 200 全量响应、ETag 变化、区间重叠、短响应、错误 Content-Range、
416 与 HTML 登录页都不会被拼进已有分段。
"""

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.manifest import ManifestEntry, load_manifest
from app.state import Entity, JobState, Segment
from app.transport import HeaderMap, RefreshError, Response, Transport, TransportError
from app.verify import (
    is_html,
    media_type_matches,
    parse_content_range,
    stream_sha256,
    strong_etag,
)

_READ_CHUNK = 64 * 1024
# 未知总长度时，首个 Range 请求的上限。
_MAX_FIRST_SPAN = 16 * 1024 * 1024


class Hooks:
    """故障注入与观察点。默认实现什么都不做。"""

    def after_segment_written(self, document_id: str, segment: Segment) -> None: ...

    def after_checkpoint_saved(self, document_id: str, state: JobState) -> None: ...

    def before_publish(self, document_id: str, state: JobState) -> None: ...


@dataclass
class DownloadResult:
    document_id: str
    ok: bool
    output_path: str | None = None
    sha256: str | None = None
    bytes_written: int = 0
    etag: str | None = None
    ranges: list[tuple[int, int]] = field(default_factory=list)
    error: str | None = None
    audit_notes: list[str] = field(default_factory=list)
    mode: str = "new"
    attempts: int = 0
    refreshes: int = 0


class FatalDownload(Exception):
    """重试无意义的失败（登录页、刷新失败、校验不通过等）。"""

    def __init__(self, message: str, *, refresh_first: bool = False) -> None:
        super().__init__(message)
        self.refresh_first = refresh_first


class RetryableDownload(Exception):
    """瞬时失败（5xx、网络中断、短响应、416 等），退避后重试。"""


class Downloader:
    def __init__(
        self,
        transport: Transport,
        runtime_dir: str | Path,
        *,
        max_connections: int = 2,
        segment_size: int = 1024 * 1024,
        max_attempts: int = 5,
        max_refreshes: int = 3,
        backoff: Callable[[int], float] | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self.transport = transport
        self.runtime_dir = Path(runtime_dir)
        self.max_connections = max(1, max_connections)
        self.segment_size = max(1, segment_size)
        self.max_attempts = max(1, max_attempts)
        self.max_refreshes = max(0, max_refreshes)
        self.backoff = backoff or (lambda attempt: min(2 ** attempt * 0.05, 2.0))
        self.hooks = hooks or Hooks()
        (self.runtime_dir / "state").mkdir(parents=True, exist_ok=True)
        (self.runtime_dir / "outputs").mkdir(parents=True, exist_ok=True)

    def job_dir(self, document_id: str) -> Path:
        return self.runtime_dir / "state" / document_id

    # ==================================================================
    # 多文档入口：连接数受限，单文档失败不影响其他文档
    # ==================================================================

    def run_manifest(self, manifest_path: str | Path) -> list[DownloadResult]:
        return self.run_entries(load_manifest(manifest_path))

    def run_entries(self, entries: list[ManifestEntry]) -> list[DownloadResult]:
        results: list[DownloadResult] = []
        with ThreadPoolExecutor(max_workers=self.max_connections) as executor:
            futures = {executor.submit(self.download, entry): entry.document_id
                       for entry in entries}
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item.document_id)
        return results

    # ==================================================================
    # 单文档下载
    # ==================================================================

    def download(self, entry: ManifestEntry) -> DownloadResult:
        job_dir = self.job_dir(entry.document_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        state = JobState.load(job_dir)
        audit_notes = state.audit(job_dir)
        state.output_name = entry.output_name or self._default_output_name(entry)

        # 上次失败或残留的成品不能继续冒充交付物。
        managed_output = state.output_path(self.runtime_dir)
        if managed_output.exists() and state.mode != "done":
            managed_output.unlink()

        url = entry.url
        last_message = "未开始"
        while state.attempts < self.max_attempts:
            state.attempts += 1
            try:
                self._attempt(entry, state, job_dir, url)
                self._publish(entry, state, job_dir)
                state.mode = "done"
                state.last_error = None
                state.save(job_dir)
                return DownloadResult(
                    document_id=entry.document_id,
                    ok=True,
                    output_path=str(state.output_path(self.runtime_dir)),
                    sha256=self._file_sha256(state.output_path(self.runtime_dir)),
                    bytes_written=state.verified_bytes,
                    etag=state.entity.etag,
                    ranges=state.covered_union(),
                    audit_notes=audit_notes,
                    mode="range" if state.entity.resumable else "full_only",
                    attempts=state.attempts,
                    refreshes=state.refreshes,
                )
            except FatalDownload as exc:
                if exc.refresh_first:
                    try:
                        url = self._refresh(entry, state, job_dir)
                    except FatalDownload as refresh_exc:
                        last_message = str(refresh_exc)
                        break
                    # 刷新成功后同轮立即重试，不额外消耗尝试次数。
                    state.attempts -= 1
                    continue
                last_message = str(exc)
                break
            except RetryableDownload as exc:
                last_message = str(exc)
                state.last_error = last_message
                state.save(job_dir)
                if state.attempts < self.max_attempts:
                    time.sleep(self.backoff(state.attempts - 1))
                continue
            except TransportError as exc:
                last_message = f"传输故障：{exc}"
                state.last_error = last_message
                state.save(job_dir)
                if state.attempts < self.max_attempts and exc.retryable:
                    time.sleep(self.backoff(state.attempts - 1))
                    continue
                break

        state.mode = "failed"
        state.last_error = last_message
        state.save(job_dir)
        # 失败时绝不留下看似完整的成品。
        state.output_path(self.runtime_dir).unlink(missing_ok=True)
        return DownloadResult(
            document_id=entry.document_id,
            ok=False,
            error=last_message,
            bytes_written=state.verified_bytes,
            etag=state.entity.etag,
            ranges=state.covered_union(),
            audit_notes=audit_notes,
            mode=state.mode,
            attempts=state.attempts,
            refreshes=state.refreshes,
        )

    # ==================================================================
    # 单次尝试
    # ==================================================================

    def _attempt(self, entry: ManifestEntry, state: JobState,
                 job_dir: Path, url: str) -> None:
        if state.mode == "done":
            return
        if state.mode == "full_only":
            self._fetch_full(entry, state, job_dir, url)
        else:  # new / range
            self._fetch_range(entry, state, job_dir, url)

        if state.entity.total_length is None:
            raise RetryableDownload("源未给出总长度，无法确认完整性")
        if not state.is_complete(state.entity.total_length):
            raise RetryableDownload("分段尚未覆盖完整文件")

    def _headers_for_range(self, state: JobState, start: int, end: int) -> dict[str, str]:
        headers = {"Range": f"bytes={start}-{end}"}
        if state.entity.etag:
            # 只有强 ETag 会被记录，因此这里永远不会发出弱校验器。
            headers["If-Range"] = state.entity.etag
        return headers

    def _fetch_range(self, entry: ManifestEntry, state: JobState,
                     job_dir: Path, url: str) -> None:
        if state.entity.total_length is not None:
            gap = state.next_gap(state.entity.total_length)
            if gap is None:
                return
            start, wanted_end = gap
            end = min(wanted_end, start + self.segment_size - 1)
        elif state.segments:
            raise RetryableDownload("已有分段但缺少总长度，无法续传")
        else:
            start = 0
            end = min(self.segment_size, _MAX_FIRST_SPAN) - 1
        response = self.transport.get(url, self._headers_for_range(state, start, end))
        self._handle_response(entry, state, job_dir, response,
                              requested=(start, end), url=url)

    def _fetch_full(self, entry: ManifestEntry, state: JobState,
                    job_dir: Path, url: str) -> None:
        response = self.transport.get(url, {})
        self._handle_response(entry, state, job_dir, response,
                              requested=None, url=url)

    # ------------------------------------------------------------------

    def _handle_response(self, entry: ManifestEntry, state: JobState,
                         job_dir: Path, response: Response,
                         requested: tuple[int, int] | None, url: str) -> None:
        status = response.status
        headers = response.headers

        if status in (401, 403, 404):
            raise FatalDownload(
                f"临时地址已失效（HTTP {status}）", refresh_first=True)
        if status == 416:
            self._handle_416(state, headers)
            return
        if status == 400:
            raise RetryableDownload("服务器拒绝区间请求（HTTP 400）")
        if status >= 500:
            raise RetryableDownload(f"服务器临时故障（HTTP {status}）")
        if status not in (200, 206):
            raise FatalDownload(f"未预期的状态码 HTTP {status}")

        with response as stream:
            prefix = stream.read(1024)
            content_type = headers.get("Content-Type")
            # 登录页可能在任何一次续传请求中返回：即使状态码 200 也不是附件。
            # 它通常意味着临时地址失效/会话过期，先刷新地址再重试。
            if is_html(prefix, content_type):
                raise FatalDownload(
                    "响应是 HTML 登录页而非附件，拒绝写入并刷新地址",
                    refresh_first=True)
            # 魔数只对实体起始字节有意义：续传分段从文件中间开始，
            # 拿不到 %PDF 等签名是正常的。200 全量响应的起点必然是 0。
            starts_at_zero = status == 200 or requested is not None and requested[0] == 0
            if starts_at_zero and not media_type_matches(
                    entry.media_type, prefix, content_type):
                raise FatalDownload(
                    f"内容与预期媒体类型 {entry.media_type} 不符"
                    f"（Content-Type={content_type or '未声明'}）")

            etag = strong_etag(headers)
            if status == 200:
                self._handle_200(state, job_dir, stream, prefix, headers, etag)
            else:
                self._handle_206(entry, state, job_dir, stream, prefix, headers,
                                 etag, requested, url)

    def _handle_200(self, state: JobState, job_dir: Path,
                    stream: object, prefix: bytes,
                    headers: HeaderMap, etag: str | None) -> None:
        length_header = headers.get("Content-Length")
        try:
            total = int(length_header) if length_header is not None else None
        except ValueError:
            total = None
        if total is None:
            raise RetryableDownload("200 响应缺少合法 Content-Length，无法防止短响应")

        # 实体变化或服务端忽略 Range：旧字节一律隔离，绝不与新实体拼接。
        if state.segments:
            if etag and state.entity.etag and etag != state.entity.etag:
                reason = "etag-changed"
            else:
                reason = "server-ignored-range"
            state.reset_for_full_download(job_dir, reason)
        else:
            state.mode = "full_only"

        # 无强校验器：保守模式，分段仅限本进程使用，退出后隔离重下。
        state.entity = Entity(etag=etag, total_length=total,
                              resumable=bool(etag))
        if not etag:
            state.entity.resumable = False
            state.mode = "full_only"
        self._write_full_body(state, job_dir, stream, prefix, total)

    def _handle_206(self, entry: ManifestEntry, state: JobState, job_dir: Path,
                    stream: object, prefix: bytes, headers: HeaderMap,
                    etag: str | None,
                    requested: tuple[int, int] | None, url: str) -> None:
        if requested is None:
            raise FatalDownload("全量请求却收到 206，协议状态异常")
        if not etag:
            # 没有强实体校验器的 206 无法保证续的是同一实体，
            # 下一轮退化为不带 Range 的全量请求（见模块文档的保守策略）。
            state.mode = "full_only"
            raise RetryableDownload("206 响应缺少强 ETag，改用全量下载")
        if state.entity.etag and etag != state.entity.etag:
            raise FatalDownload("ETag 已变化，旧分段属于不同实体")

        content_range = parse_content_range(headers.get("Content-Range"))
        if content_range is None:
            raise RetryableDownload("206 响应缺少合法 Content-Range")
        req_start, req_end = requested
        if content_range.start != req_start:
            raise FatalDownload(
                f"Content-Range 起点 {content_range.start} 与请求 {req_start} 不符")
        if content_range.end > req_end:
            raise FatalDownload("Content-Range 超出请求区间")
        if content_range.total is None:
            raise RetryableDownload("Content-Range 未给出总长度")
        total = content_range.total

        if state.entity.total_length is None:
            state.entity.total_length = total
        elif state.entity.total_length != total:
            raise FatalDownload(
                f"实体总长度变化：{state.entity.total_length} -> {total}")
        if content_range.end >= total:
            raise FatalDownload("Content-Range 越过实体总长度")

        declared_length = headers.get("Content-Length")
        if declared_length is not None:
            try:
                if int(declared_length) != content_range.length:
                    raise FatalDownload("Content-Length 与 Content-Range 长度不一致")
            except ValueError:
                raise FatalDownload("Content-Length 不是整数")

        # 与已验证分段重叠的内容不能直接拼进旧文件。
        for seg_start, seg_end in state.covered_union():
            if content_range.start <= seg_end and content_range.end >= seg_start:
                raise FatalDownload(
                    f"新区间 {content_range.start}-{content_range.end} "
                    f"与已验证分段 {seg_start}-{seg_end} 重叠")

        if state.entity.etag is None:
            state.entity.etag = etag
            state.entity.resumable = True
            state.mode = "range"

        self._drain_segment(state, job_dir, stream, prefix,
                            content_range.start, content_range.end,
                            content_range.length)
        state.save(job_dir)
        self.hooks.after_checkpoint_saved(state.document_id, state)

        # 同一实体还有缺口就继续拉，直到覆盖完整文件或遇到下一次故障。
        gap = state.next_gap(total)
        if gap is not None:
            start, wanted_end = gap
            end = min(wanted_end, start + self.segment_size - 1)
            response = self.transport.get(
                url, self._headers_for_range(state, start, end))
            self._handle_response(entry, state, job_dir, response,
                                  requested=(start, end), url=url)

    def _handle_416(self, state: JobState, headers: HeaderMap) -> None:
        content_range = parse_content_range(headers.get("Content-Range"))
        if content_range and content_range.total is not None:
            total = content_range.total
            if state.entity.total_length is not None and \
                    state.entity.total_length != total:
                raise FatalDownload(
                    f"416 宣告的总长度 {total} 与检查点 {state.entity.total_length} 不符")
            state.entity.total_length = total
            # 只有分段确实连续覆盖整个实体时，416 才意味着请求越过 EOF。
            if state.is_complete(total):
                return
        raise RetryableDownload("请求区间不可满足（HTTP 416）")

    # ------------------------------------------------------------------
    # 正文写出：短响应在分段长度/哈希阶段即被发现
    # ------------------------------------------------------------------

    def _drain_segment(self, state: JobState, job_dir: Path,
                       stream: object, prefix: bytes,
                       start: int, end: int, expected: int) -> Segment:
        """落盘一个 206 分段。

        ``prefix`` 是媒体检查预读、且仍属于本分段开头的字节，只写一次。
        """
        segments_dir = job_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        if len(prefix) > expected:
            raise FatalDownload("响应正文长于 Content-Range 声明长度")
        name = f"seg-{start:012d}-{end:012d}"
        tmp = segments_dir / f"{name}.tmp"
        digest = hashlib.sha256()
        received = 0
        try:
            with tmp.open("wb") as handle:
                handle.write(prefix)
                digest.update(prefix)
                received += len(prefix)
                while received < expected:
                    chunk = stream.read(min(_READ_CHUNK, expected - received))
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
            if received != expected:
                raise RetryableDownload(
                    f"短响应：区间 {start}-{end} 期望 {expected} 字节，实际 {received}")
            destination = segments_dir / name
            os.replace(tmp, destination)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        segment = Segment(start=start, end=end,
                          sha256=digest.hexdigest(), file=name)
        state.segments.append(segment)
        state.segments.sort(key=lambda item: item.start)
        state.verified_bytes = sum(item.end - item.start + 1 for item in state.segments)
        self.hooks.after_segment_written(state.document_id, segment)
        return segment

    def _write_full_body(self, state: JobState, job_dir: Path,
                         stream: object, prefix: bytes, total: int) -> None:
        """全量响应按 segment_size 切成不可变分段，逐段检查点。

        媒体检查预读的前缀作为第一段的开头写入，不丢弃不重复。
        """
        segments_dir = job_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        start = 0
        received_total = 0
        tmp: Path | None = None
        try:
            while received_total < total:
                segment_end = min(start + self.segment_size - 1, total - 1)
                expected_segment = segment_end - start + 1
                name = f"seg-{start:012d}-{segment_end:012d}"
                tmp = segments_dir / f"{name}.tmp"
                segment_digest = hashlib.sha256()
                written = 0
                with tmp.open("wb") as handle:
                    if prefix:
                        # 预读前缀只属于第一个分段。
                        chunk = prefix[:expected_segment]
                        handle.write(chunk)
                        segment_digest.update(chunk)
                        written += len(chunk)
                        received_total += len(chunk)
                        prefix = prefix[len(chunk):]
                    while written < expected_segment:
                        chunk = stream.read(min(_READ_CHUNK,
                                                expected_segment - written))
                        if not chunk:
                            break
                        handle.write(chunk)
                        segment_digest.update(chunk)
                        written += len(chunk)
                        received_total += len(chunk)
                if written != expected_segment:
                    raise RetryableDownload(
                        f"短响应：分段 {start}-{segment_end} 期望 "
                        f"{expected_segment} 字节，实际 {written}")
                destination = segments_dir / name
                os.replace(tmp, destination)
                tmp = None
                segment = Segment(start=start, end=segment_end,
                                  sha256=segment_digest.hexdigest(), file=name)
                state.segments.append(segment)
                state.segments.sort(key=lambda item: item.start)
                state.verified_bytes = received_total
                self.hooks.after_segment_written(state.document_id, segment)
                state.save(job_dir)
                self.hooks.after_checkpoint_saved(state.document_id, state)
                start = segment_end + 1
        except BaseException:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            raise

        if received_total != total:
            raise RetryableDownload(
                f"短响应：实体期望 {total} 字节，实际 {received_total}")
        state.verified_bytes = received_total

    # ------------------------------------------------------------------
    # 刷新与发布
    # ------------------------------------------------------------------

    def _refresh(self, entry: ManifestEntry, state: JobState, job_dir: Path) -> str:
        if state.refreshes >= self.max_refreshes:
            raise FatalDownload(
                f"刷新次数已达上限 {self.max_refreshes}，地址仍不可用")
        if not entry.refresh_entry:
            raise FatalDownload("下载地址已失效且清单未提供刷新入口")
        state.refreshes += 1
        try:
            new_url = self.transport.refresh(entry.document_id, entry.refresh_entry)
        except RefreshError as exc:
            state.last_error = str(exc)
            state.save(job_dir)
            raise FatalDownload(str(exc)) from exc
        state.last_error = None
        state.save(job_dir)
        return new_url

    def _publish(self, entry: ManifestEntry, state: JobState, job_dir: Path) -> None:
        total = state.entity.total_length
        assert total is not None
        if not state.is_complete(total):
            raise RetryableDownload("分段不连续，拒绝发布")
        self.hooks.before_publish(state.document_id, state)
        staging = state.assemble(job_dir, self.runtime_dir)
        try:
            actual_length = staging.stat().st_size
            if actual_length != total:
                raise FatalDownload(
                    f"暂存文件长度 {actual_length} 与实体长度 {total} 不符")
            with staging.open("rb") as handle:
                actual_digest = stream_sha256(handle)
            if entry.expected_digest and actual_digest != entry.expected_digest:
                raise FatalDownload(
                    f"SHA-256 不匹配：期望 {entry.expected_digest}，实际 {actual_digest}")
            state.publish(self.runtime_dir, staging)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    @staticmethod
    def _file_sha256(path: Path) -> str | None:
        if not path.exists():
            return None
        with path.open("rb") as handle:
            return stream_sha256(handle)

    @staticmethod
    def _default_output_name(entry: ManifestEntry) -> str:
        suffix = {
            "application/pdf": ".pdf",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "application/zip": ".zip",
        }.get(entry.media_type.split(";", 1)[0].strip().lower(), ".bin")
        return f"{entry.document_id}{suffix}"

    # ==================================================================
    # 操作人员查询清单
    # ==================================================================

    def build_report(self, entries: list[ManifestEntry]) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for entry in entries:
            job_dir = self.job_dir(entry.document_id)
            checkpoint = job_dir / "checkpoint.json"
            item: dict[str, Any] = {
                "document_id": entry.document_id,
                "media_type": entry.media_type,
                "expected_sha256": entry.expected_digest,
                "status": "never_started",
                "etag": None,
                "resumable": False,
                "total_length": None,
                "verified_ranges": [],
                "verified_bytes": 0,
                "output": None,
                "output_sha256": None,
                "attempts": 0,
                "refreshes": 0,
                "failure_reason": None,
                "audit_notes": [],
                "integrity_note": None,
            }
            if checkpoint.exists():
                state = JobState.load(job_dir)
                output = state.output_path(self.runtime_dir)
                item.update({
                    "status": state.mode,
                    "etag": state.entity.etag,
                    "resumable": state.entity.resumable,
                    "total_length": state.entity.total_length,
                    "verified_ranges": state.covered_union(),
                    "verified_bytes": state.verified_bytes,
                    "attempts": state.attempts,
                    "refreshes": state.refreshes,
                    "failure_reason": state.last_error,
                    "audit_notes": state.audit(job_dir),
                })
                if output.exists():
                    item["output"] = str(output)
                    item["output_sha256"] = self._file_sha256(output)
                    if entry.expected_digest:
                        item["integrity_note"] = (
                            "已通过长度与清单 SHA-256 校验"
                            if item["output_sha256"] == entry.expected_digest
                            else "警告：成品 SHA-256 与清单不符")
                    else:
                        item["integrity_note"] = (
                            "清单未提供 SHA-256：仅验证长度与分段哈希链，"
                            "无法独立证明实体真伪")
                # 报告只读：隔离区中已有物证作为审计历史呈现，不再触发审计。
                quarantine_dir = job_dir / "quarantine"
                if quarantine_dir.exists():
                    item["audit_notes"] = sorted(
                        path.name for path in quarantine_dir.iterdir()
                        if path.is_file())
            report.append(item)
        return report
