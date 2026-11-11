"""传输抽象与两种实现。

引擎只依赖 :class:`Transport` 协议，不直接认识合成端点或 urllib：
* :class:`ScriptedTransport` 基于现有 ``AttachmentEndpoint``，按 URL 重放故障，
  供离线演示与测试使用；
* :class:`HttpTransport` 基于标准库 urllib，用于真实的已授权来源。

所有正文都以只读流的方式暴露，引擎按固定小块写出，避免整文件驻留内存。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from app.attachment import AttachmentEndpoint


class TransportError(Exception):
    """传输层基础异常，按是否可重试细分。"""

    retryable = False


class NetworkError(TransportError):
    """连接中断、超时等瞬时故障，可重试。"""

    retryable = True


class RefreshError(TransportError):
    """临时地址刷新失败；只应阻塞对应文档。"""

    retryable = False


class HeaderMap:
    """不区分大小写的只读响应头映射。"""

    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        self._store: dict[str, str] = {}
        for key, value in (headers or {}).items():
            self._store[key.lower()] = value

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._store.get(name.lower(), default)

    def items(self) -> list[tuple[str, str]]:
        return list(self._store.items())

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._store

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"HeaderMap({self._store!r})"


@dataclass
class Response:
    """一次 GET 的结果。

    ``stream`` 返回上下文管理器，进入后得到只读二进制流；消费方必须关闭，
    以便 HTTP 连接能够归还连接池。
    """

    status: int
    headers: HeaderMap
    stream: Any

    def __enter__(self) -> Any:
        return self.stream.__enter__()

    def __exit__(self, *exc: object) -> None:
        self.stream.__exit__(*exc)


class _MemoryStream:
    """把内存字节伪装成只读流，供合成端点使用。"""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._closed = False

    def __enter__(self) -> "_MemoryStream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("流已关闭")
        if size is None or size < 0:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self) -> None:
        self._closed = True


class Transport(Protocol):
    """引擎依赖的最小传输接口。"""

    def get(self, url: str, headers: Mapping[str, str]) -> Response: ...

    def refresh(self, document_id: str, entry: str) -> str: ...


class ScriptedTransport:
    """复用 :class:`AttachmentEndpoint` 的离线传输。

    故障按 document_id 配置：
    * ``behaviors`` 中每个条目可以是固定行为，或 ``{"times": n, "then": x}``
      形式的序列（例如先短响应两次再恢复正常）；
    * ``refresh_fail_ids`` 中的文档刷新入口会抛 :class:`RefreshError`，
      用于验证单个文档失败不影响其他文档。
    """

    def __init__(
        self,
        fixture_root: str = "fixtures",
        behaviors: Mapping[str, Any] | None = None,
        refresh_fail_ids: set[str] | None = None,
    ) -> None:
        self.endpoint = AttachmentEndpoint(fixture_root)
        self._behavior_specs: dict[str, list[Any]] = {}
        for doc_id, spec in (behaviors or {}).items():
            self._behavior_specs[doc_id] = spec if isinstance(spec, list) else [spec]
        self._refresh_fail_ids = set(refresh_fail_ids or ())

    @staticmethod
    def _doc_id(url: str) -> str:
        # 合成 URL 形如 https://attachment.invalid/<id>?temporary=...
        marker = "attachment.invalid/"
        rest = url.split(marker, 1)[1]
        return rest.split("?", 1)[0]

    def _next_behavior(self, document_id: str) -> str:
        spec = self._behavior_specs.get(document_id, ["normal"])
        if len(spec) == 1 and not isinstance(spec[0], dict):
            return spec[0]
        current = spec.pop(0)
        if isinstance(current, dict):
            current["times"] -= 1
            behavior = current["behavior"]
            if current["times"] > 0:
                spec.insert(0, current)
            else:
                spec.insert(0, current["then"])
            return behavior
        return current

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        document_id = self._doc_id(url)
        behavior = self._next_behavior(document_id)
        range_header = headers.get("Range")
        if_range = headers.get("If-Range")
        reply = self.endpoint.fetch(document_id, range_header, if_range, behavior)
        return Response(reply.status, HeaderMap(reply.headers), _MemoryStream(reply.body))

    def refresh(self, document_id: str, entry: str) -> str:
        if document_id in self._refresh_fail_ids:
            raise RefreshError(f"文档 {document_id} 的刷新入口返回 503")
        return self.endpoint.refresh(document_id)


class HttpTransport:
    """标准库实现的真实 HTTP 传输，只使用 GET。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        request = urllib_request.Request(url, headers=dict(headers), method="GET")
        try:
            handle = urllib_request.urlopen(request, timeout=self.timeout)
        except urllib_error.HTTPError as exc:
            # HTTPError 自身可作为响应体读取，保留状态码交给引擎决策。
            raw = exc.headers or {}
            mapped = {key: raw[key] for key in raw.keys()} if hasattr(raw, "keys") else {}
            return Response(exc.code, HeaderMap(mapped), _HttpBody(exc))
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise NetworkError(f"无法连接 {url}: {exc}") from exc
        return Response(
            handle.status,
            HeaderMap({key: handle.headers[key] for key in handle.headers.keys()}),
            _HttpBody(handle),
        )

    def refresh(self, document_id: str, entry: str) -> str:
        """真实环境的刷新入口由站点决定，默认按 JSON 契约读取。"""
        return http_refresh_json(self, entry)


class _HttpBody:
    """urllib 响应对象的上下文包装。"""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def __enter__(self) -> "_HttpBody":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def close(self) -> None:
        self._handle.close()


def http_refresh_json(transport: Transport, entry: str) -> str:
    """刷新入口约定：GET 后返回 ``{"url": "..."}``。

    刷新入口本身也可能过期或故障；失败统一转为 :class:`RefreshError`，
    调用方据此只阻塞对应文档而不触碰其他文档的进度。
    """
    try:
        response = transport.get(entry, {"Accept": "application/json"})
    except TransportError as exc:
        raise RefreshError(f"刷新入口不可用：{entry}（{exc}）") from exc
    with response as stream:
        if response.status != 200:
            raise RefreshError(f"刷新入口返回 {response.status}：{entry}")
        payload = stream.read()
    try:
        url = json.loads(payload.decode("utf-8"))["url"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RefreshError(f"刷新入口响应不是预期 JSON：{entry}") from exc
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RefreshError(f"刷新入口未给出合法 URL：{entry}")
    return url
