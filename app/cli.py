"""命令行入口：下载附件清单、按 document_id 查询下载状态。

用法见 README。脚本化传输读取的故障配置是可信的本地文件；下载得到的
字节全部落在独立的 runtime 目录中。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.engine import Downloader, Hooks, Segment
from app.manifest import load_manifest
from app.state import JobState
from app.transport import HttpTransport, ScriptedTransport


class KillHooks(Hooks):
    """演示用故障注入：在指定事件点杀死当前进程，模拟断电/被杀。"""

    def __init__(self, kill: str | None, document_id: str | None = None) -> None:
        self.kill = kill
        self.document_id = document_id

    def _maybe_kill(self, event: str, document_id: str) -> None:
        if self.kill == event and (
                self.document_id is None or self.document_id == document_id):
            sys.stderr.write(
                f"[kill-hook] 已在事件 {event}（document_id={document_id}）终止进程\n")
            sys.stderr.flush()
            import signal
            os_kill = __import__("os").kill
            os_kill(__import__("os").getpid(), signal.SIGKILL)

    def after_segment_written(self, document_id: str, segment: Segment) -> None:
        self._maybe_kill("after_segment", document_id)

    def after_checkpoint_saved(self, document_id: str, state: JobState) -> None:
        self._maybe_kill("after_checkpoint", document_id)

    def before_publish(self, document_id: str, state: JobState) -> None:
        self._maybe_kill("before_publish", document_id)


def _load_script_config(path: str | None) -> tuple[dict, set]:
    if not path:
        return {}, set()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return raw.get("behaviors", {}), set(raw.get("refresh_fail_ids", []))


def build_downloader(args: argparse.Namespace) -> Downloader:
    if args.transport == "http":
        transport = HttpTransport(timeout=args.timeout)
    else:
        behaviors, refresh_fail = _load_script_config(args.script_config)
        transport = ScriptedTransport(
            fixture_root=args.fixture_root,
            behaviors=behaviors,
            refresh_fail_ids=refresh_fail,
        )
    hooks = KillHooks(args.kill, args.kill_document) if args.kill else Hooks()
    return Downloader(
        transport,
        args.runtime,
        max_connections=args.max_connections,
        segment_size=args.segment_size,
        max_attempts=args.max_attempts,
        max_refreshes=args.max_refreshes,
        hooks=hooks,
    )


def cmd_download(args: argparse.Namespace) -> int:
    entries = load_manifest(args.manifest)
    if args.document_id:
        wanted = set(args.document_id)
        entries = [entry for entry in entries if entry.document_id in wanted]
        if len(entries) != len(wanted):
            missing = wanted - {entry.document_id for entry in entries}
            print(f"清单中找不到 document_id：{', '.join(sorted(missing))}",
                  file=sys.stderr)
            return 2
    downloader = build_downloader(args)
    results = downloader.run_entries(entries)
    if args.json:
        print(json.dumps(
            [result.__dict__ for result in results],
            ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = "成功" if result.ok else f"失败：{result.error}"
            print(f"[{result.document_id}] {status}")
            if result.ok:
                print(f"    成品：{result.output_path}")
                print(f"    SHA-256：{result.sha256}")
                print(f"    已验证区间：{result.ranges}（{result.bytes_written} 字节）")
    return 0 if all(result.ok for result in results) else 1


def cmd_report(args: argparse.Namespace) -> int:
    entries = load_manifest(args.manifest)
    if args.document_id:
        wanted = set(args.document_id)
        entries = [entry for entry in entries if entry.document_id in wanted]
        if len(entries) != len(wanted):
            missing = wanted - {entry.document_id for entry in entries}
            print(f"清单中找不到 document_id：{', '.join(sorted(missing))}",
                  file=sys.stderr)
            return 2
    downloader = build_downloader(args)
    report = downloader.build_report(entries)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for item in report:
        print(f"document_id : {item['document_id']}")
        print(f"  状态            : {item['status']}")
        print(f"  实体 ETag       : {item['etag'] or '（无强校验器）'}")
        print(f"  可跨进程续传    : {'是' if item['resumable'] else '否'}")
        print(f"  总长度          : {item['total_length']}")
        print(f"  已验证字节范围  : {item['verified_ranges']}")
        print(f"  已验证字节数    : {item['verified_bytes']}")
        print(f"  成品路径        : {item['output'] or '（未发布）'}")
        print(f"  成品 SHA-256    : {item['output_sha256'] or '（无）'}")
        print(f"  清单 SHA-256    : {item['expected_sha256'] or '（未提供）'}")
        print(f"  完整性结论      : {item['integrity_note'] or '（尚无成品）'}")
        print(f"  尝试/刷新次数   : {item['attempts']} / {item['refreshes']}")
        if item["failure_reason"]:
            print(f"  失败原因        : {item['failure_reason']}")
        if item["audit_notes"]:
            print(f"  启动审计        : {'; '.join(item['audit_notes'])}")
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="procure-download",
        description="采购附件的可校验分段下载组件（仅采集公开/已授权附件）")
    parser.add_argument("--manifest", default="fixtures/manifest.json",
                        help="附件清单 JSON 路径")
    parser.add_argument("--runtime", default="runtime",
                        help="运行目录（分段、检查点、成品、隔离区）")
    parser.add_argument("--transport", choices=("script", "http"),
                        default="script", help="script=合成端点演示，http=真实站点")
    parser.add_argument("--fixture-root", default="fixtures",
                        help="script 传输使用的素材目录")
    parser.add_argument("--script-config",
                        help="script 传输的故障配置 JSON")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-connections", type=int, default=2,
                        help="同时占用的连接数上限")
    parser.add_argument("--segment-size", type=int, default=1024 * 1024)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--max-refreshes", type=int, default=3)
    parser.add_argument("--kill",
                        choices=("after_segment", "after_checkpoint",
                                 "before_publish"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--kill-document", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)
    p_download = sub.add_parser("download", help="下载清单中的附件")
    p_download.add_argument("--document-id", action="append",
                            help="只下载指定 document_id，可重复")
    p_download.add_argument("--json", action="store_true",
                            help="以 JSON 输出结果")
    p_download.set_defaults(func=cmd_download)

    p_report = sub.add_parser("report", help="按 document_id 查询下载清单")
    p_report.add_argument("--document-id", action="append",
                          help="只查询指定 document_id，可重复")
    p_report.add_argument("--json", action="store_true",
                          help="以 JSON 输出报告")
    p_report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
