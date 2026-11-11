"""每个文档的本地状态：不可变分段、检查点与隔离区。

目录布局（全部位于运行目录，与原始素材分离）::

    <runtime>/state/<document_id>/
        checkpoint.json        原子替换的检查点
        segments/seg-<start>-<end>   只增不改的分段文件
        quarantine/...         损坏、过期或崩溃残留的字节，仅留证不参与拼装
    <runtime>/outputs/<name>   仅在校验全部通过后原子发布

关键不变量：
* 分段先写临时文件、算哈希，再原子改名并随后写入检查点；崩溃只会留下
  没有检查点记录的临时文件，下次启动一律隔离；
* 已落盘的分段永不追加、永不就地修改，引擎只能整体替换；
* 检查点记录每个分段的 SHA-256，恢复时逐段复核，被篡改的字节进隔离区。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.verify import bytes_sha256

_WRITE_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Segment:
    start: int
    end: int  # 闭区间
    sha256: str
    file: str


@dataclass
class Entity:
    """当前下载所依据的实体信息；URL 换了但实体没变就可以继续。"""

    etag: str | None = None
    total_length: int | None = None
    resumable: bool = False  # 只有拿到过强 ETag 才为 True


@dataclass
class JobState:
    document_id: str
    mode: str = "new"  # new | range | full_only | done | failed
    entity: Entity = field(default_factory=Entity)
    segments: list[Segment] = field(default_factory=list)
    output_name: str | None = None
    last_error: str | None = None
    attempts: int = 0
    refreshes: int = 0
    verified_bytes: int = 0

    # ---- 持久化 -------------------------------------------------------

    @classmethod
    def load(cls, job_dir: Path) -> "JobState":
        checkpoint = job_dir / "checkpoint.json"
        state = cls(document_id=job_dir.name)
        if not checkpoint.exists():
            return state
        raw = json.loads(checkpoint.read_text(encoding="utf-8"))
        state.mode = raw["mode"]
        state.entity = Entity(**raw["entity"])
        state.segments = [Segment(**item) for item in raw["segments"]]
        state.output_name = raw.get("output_name")
        state.last_error = raw.get("last_error")
        state.attempts = raw.get("attempts", 0)
        state.refreshes = raw.get("refreshes", 0)
        state.verified_bytes = raw.get("verified_bytes", 0)
        return state

    def save(self, job_dir: Path) -> None:
        payload = {
            "document_id": self.document_id,
            "mode": self.mode,
            "entity": asdict(self.entity),
            "segments": [asdict(segment) for segment in self.segments],
            "output_name": self.output_name,
            "last_error": self.last_error,
            "attempts": self.attempts,
            "refreshes": self.refreshes,
            "verified_bytes": self.verified_bytes,
        }
        checkpoint = job_dir / "checkpoint.json"
        tmp = checkpoint.with_name("checkpoint.json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, checkpoint)

    # ---- 启动审计 -----------------------------------------------------

    def audit(self, job_dir: Path) -> list[str]:
        """恢复前审计本地字节，返回隔离原因列表。"""
        notes: list[str] = []
        segments_dir = job_dir / "segments"
        quarantine_dir = job_dir / "quarantine"

        # 1. 崩溃残留的临时文件没有检查点背书，直接隔离。
        for tmp_path in sorted(segments_dir.glob("*.tmp")) if segments_dir.exists() else []:
            notes.append(self._quarantine(quarantine_dir, tmp_path, "crash-temp"))

        # 2. 已改名但检查点未记录（或检查点不存在）的孤儿分段同样没有背书，
        #    可能只写了一半或属于未知实体，一律隔离重下。
        referenced = {segment.file for segment in self.segments}
        if segments_dir.exists():
            for seg_path in sorted(segments_dir.glob("seg-*")):
                if seg_path.name not in referenced:
                    notes.append(self._quarantine(
                        quarantine_dir, seg_path, f"orphan-{seg_path.name}"))

        # 3. 没有强校验器的旧分段不能跨进程续传，整体隔离后全量重下。
        if self.segments and not self.entity.resumable:
            for segment in list(self.segments):
                target = segments_dir / segment.file
                if target.exists():
                    notes.append(self._quarantine(
                        quarantine_dir, target, "no-strong-validator"))
            self.segments.clear()

        # 4. 逐段复核存在性、区间合法性与哈希。
        kept: list[Segment] = []
        for segment in self.segments:
            target = segments_dir / segment.file
            if not target.exists():
                notes.append(f"分段 {segment.file} 缺失，丢弃记录")
                continue
            if segment.end < segment.start:
                notes.append(self._quarantine(
                    quarantine_dir, target, f"bad-range-{segment.file}"))
                continue
            digest = bytes_sha256(target.read_bytes())
            if digest != segment.sha256:
                notes.append(self._quarantine(
                    quarantine_dir, target, f"tampered-{segment.file}"))
                continue
            kept.append(segment)
        self.segments = kept

        # 5. 区间重叠或乱序属于状态损坏，保守地隔离全部并重新开始。
        ordered = sorted(self.segments, key=lambda item: item.start)
        overlap = any(prev.end + 1 != cur.start
                      for prev, cur in zip(ordered, ordered[1:]))
        if overlap:
            for segment in ordered:
                target = segments_dir / segment.file
                if target.exists():
                    notes.append(self._quarantine(
                        quarantine_dir, target, f"overlap-{segment.file}"))
            self.segments.clear()
            self.entity = Entity()
            self.mode = "new"
        else:
            self.segments = ordered

        # 6. 审计若隔离过任何背书分段，failed/done 等终态不再成立；
        #    仍有强校验器与总长度就回到 range 续传，否则回到 new 重新协商。
        if notes and self.mode in ("failed", "done"):
            if self.entity.resumable and self.entity.total_length is not None:
                self.mode = "range"
            else:
                self.mode = "new"

        self.verified_bytes = sum(item.end - item.start + 1 for item in self.segments)
        if notes:
            self.save(job_dir)
        return notes

    @staticmethod
    def _quarantine(quarantine_dir: Path, target: Path, reason: str) -> str:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{reason}-{target.name}"
        destination = quarantine_dir / stamp
        counter = 1
        while destination.exists():
            destination = quarantine_dir / f"{stamp}.{counter}"
            counter += 1
        os.replace(target, destination)
        return f"已隔离 {target.name}：{reason}"

    # ---- 分段写入与替换 ----------------------------------------------

    def write_segment(self, job_dir: Path, start: int, end: int,
                      stream: object) -> Segment:
        """从响应流读取确定长度的字节，落盘为不可变分段。"""
        expected = end - start + 1
        segments_dir = job_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        name = f"seg-{start:012d}-{end:012d}"
        tmp = segments_dir / f"{name}.tmp"
        digest = hashlib.sha256()
        received = 0
        try:
            with tmp.open("wb") as handle:
                while received < expected:
                    chunk = stream.read(min(_WRITE_CHUNK, expected - received))  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
            if received != expected:
                raise ValueError(f"短响应：期望 {expected} 字节，实际 {received}")
            destination = segments_dir / name
            os.replace(tmp, destination)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        segment = Segment(start=start, end=end, sha256=digest.hexdigest(), file=name)
        self.segments.append(segment)
        self.segments.sort(key=lambda item: item.start)
        self.verified_bytes = sum(item.end - item.start + 1 for item in self.segments)
        return segment

    def reset_for_full_download(self, job_dir: Path, reason: str) -> None:
        """实体变化或服务端忽略 Range 时，旧字节全部隔离，绝不拼进新文件。"""
        segments_dir = job_dir / "segments"
        quarantine_dir = job_dir / "quarantine"
        for segment in list(self.segments):
            target = segments_dir / segment.file
            if target.exists():
                self._quarantine(quarantine_dir, target, reason)
        self.segments.clear()
        self.verified_bytes = 0
        self.entity = Entity()
        self.mode = "full_only"

    # ---- 区间查询 -----------------------------------------------------

    def covered_union(self) -> list[tuple[int, int]]:
        """返回已验证分段的并集（审计保证互不重叠且有序）。"""
        return [(item.start, item.end) for item in self.segments]

    def next_gap(self, total_length: int) -> tuple[int, int] | None:
        """给定总长度，返回第一个未覆盖区间 [start, end]，全部覆盖返回 None。

        分段可能因隔离而不包含 0 起点，不能假设它们从 0 无缝衔接。
        """
        cursor = 0
        for segment in self.segments:
            if segment.start > cursor:
                return cursor, segment.start - 1
            if segment.end + 1 > cursor:
                cursor = segment.end + 1
        if cursor < total_length:
            return cursor, total_length - 1
        return None

    def is_complete(self, total_length: int) -> bool:
        if not self.segments or self.segments[0].start != 0:
            return False
        if self.segments[-1].end != total_length - 1:
            return False
        return all(prev.end + 1 == cur.start
                   for prev, cur in zip(self.segments, self.segments[1:]))

    # ---- 发布 ---------------------------------------------------------

    def output_path(self, runtime_dir: Path) -> Path:
        return runtime_dir / "outputs" / (self.output_name or f"{self.document_id}.bin")

    def staging_path(self, runtime_dir: Path) -> Path:
        outputs = runtime_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        return outputs / f".{self.document_id}.staging"

    def assemble(self, job_dir: Path, runtime_dir: Path) -> Path:
        """把已验证分段拼装到暂存文件，返回暂存路径；不直接发布成品。"""
        staging = self.staging_path(runtime_dir)
        segments_dir = job_dir / "segments"
        with staging.open("wb") as out:
            for segment in self.segments:
                with (segments_dir / segment.file).open("rb") as handle:
                    while True:
                        chunk = handle.read(_WRITE_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
        return staging

    def publish(self, runtime_dir: Path, staging: Path) -> Path:
        target = self.output_path(runtime_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)  # 原子改名，成品要么完整要么不存在
        return target
