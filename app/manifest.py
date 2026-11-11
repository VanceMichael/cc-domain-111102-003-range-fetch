"""附件下载清单的读取与校验。

清单描述文件身份（document_id、预期媒体类型、可选 SHA-256、刷新入口）
和一个临时下载 URL。URL 只是暂时位置，引擎不把它当作身份。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManifestEntry:
    document_id: str
    media_type: str
    url: str
    refresh_entry: str | None = None
    sha256: str | None = None
    output_name: str | None = None

    @property
    def expected_digest(self) -> str | None:
        if not self.sha256:
            return None
        digest = self.sha256.lower().strip()
        return digest if len(digest) == 64 else None


def load_manifest(path: str | Path) -> list[ManifestEntry]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("documents"), list):
        raise ValueError("清单顶层必须是包含 documents 数组的对象")
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for item in raw["documents"]:
        if not isinstance(item, dict):
            raise ValueError("documents 中的每个条目必须是对象")
        document_id = str(item.get("document_id", "")).strip()
        media_type = str(item.get("media_type", "")).strip()
        url = str(item.get("url", "")).strip()
        if not document_id or not media_type or not url:
            raise ValueError("document_id、media_type、url 均为必填项")
        if document_id in seen:
            raise ValueError(f"document_id 重复：{document_id}")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"{document_id} 的 url 必须是 http(s) 地址")
        sha256 = item.get("sha256")
        if sha256 is not None:
            sha256 = str(sha256).lower().strip()
            if len(sha256) != 64:
                raise ValueError(f"{document_id} 的 sha256 必须是 64 位十六进制")
        seen.add(document_id)
        entries.append(
            ManifestEntry(
                document_id=document_id,
                media_type=media_type,
                url=url,
                refresh_entry=(str(item["refresh_entry"]).strip()
                               if item.get("refresh_entry") else None),
                sha256=sha256,
                output_name=(str(item["output_name"]).strip()
                             if item.get("output_name") else None),
            )
        )
    if not entries:
        raise ValueError("清单中没有任何文档")
    return entries
