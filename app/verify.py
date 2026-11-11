"""实体校验器、Content-Range 与媒体类型检查。

下载组件的安全边界集中在这里：
* 只有强 ETag 才能作为 If-Range 实体校验器；
* Content-Range 的起止、总长度必须与请求和实际字节数一致；
* 预期媒体类型用魔数复核，HTML 登录页即使 200 也必须被识破；
* SHA-256 以流式方式计算，不把整文件读入内存。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# 常见采购附件类型的文件签名。未列出的类型跳过魔数检查，只做 HTML 拒绝。
_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "application/x-rar-compressed": (b"Rar!\x1a\x07",),
    "application/x-7z-compressed": (b"7z\xbc\xaf\x27\x1c",),
    "application/octet-stream": (),
}

_HTML_TOKENS = (b"<!doctype html", b"<html", b"<head", b"<title", b"<body", b"<form")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_CHUNK_SIZE = 64 * 1024


def strong_etag(headers: object) -> str | None:
    """返回可用于 If-Range 的强 ETag；弱 ETag（W/）一律不接受。"""
    value = headers.get("ETag")  # type: ignore[attr-defined]
    if not value:
        return None
    value = value.strip()
    if value.startswith(("W/", "w/")):
        return None
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value
    return None


@dataclass(frozen=True)
class ContentRange:
    start: int
    end: int
    total: int | None  # None 对应 "bytes start-end/*"

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_content_range(value: str | None) -> ContentRange | None:
    """严格解析 Content-Range，无法识别时返回 None（拒绝而不是猜测）。"""
    if not value:
        return None
    match = _CONTENT_RANGE_RE.fullmatch(value.strip())
    if not match:
        return None
    start, end = int(match[1]), int(match[2])
    if end < start:
        return None
    total = None if match[3] == "*" else int(match[3])
    if total is not None and (end >= total or start >= total):
        return None
    return ContentRange(start, end, total)


def is_html(body_prefix: bytes, content_type: str | None) -> bool:
    """识别登录页等 HTML 响应。"""
    if content_type and "html" in content_type.lower():
        return True
    head = body_prefix[:1024].lstrip()[:512].lower()
    return any(token in head for token in _HTML_TOKENS)


def media_type_matches(media_type: str, body_prefix: bytes, content_type: str | None) -> bool:
    """用预期媒体类型和魔数双重确认正文不是登录页或错配内容。"""
    if is_html(body_prefix, content_type):
        return False
    expected = media_type.split(";", 1)[0].strip().lower()
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared not in (expected, "application/octet-stream"):
        return False
    signatures = _MAGIC_SIGNATURES.get(expected)
    if not signatures:
        # 未收录签名的类型：HTML 已拒绝，声明类型一致即放行，
        # 最终仍由长度与可选 SHA-256 兜底。
        return True
    return any(body_prefix.startswith(signature) for signature in signatures)


def stream_sha256(stream: object) -> str:
    """流式计算 SHA-256，调用方负责流的打开与关闭。"""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(_CHUNK_SIZE)  # type: ignore[attr-defined]
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
