"""崩溃与篡改演示：每个阶段都在全新进程中运行下载器。

运行方式（-I 隔离，项目根由启动器显式加入 sys.path）::

    python3 -I scripts/run_demo.py

阶段：
1. 写完分段后被杀（SIGKILL），新进程从已有分段恢复；
2. 记录检查点后被杀，新进程恢复并继续；
3. 发布成品前被杀，新进程重新组装并原子发布；
4. 一个文档刷新失败，只阻塞该文档，另一个文档照常完成；
5. 全部完成后独立校验成品（长度、SHA-256、魔数）；
6. 篡改已有分段，新进程审计发现哈希不符，隔离并重新下载。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME = PROJECT_ROOT / "runtime" / "demo"
FIXTURES = PROJECT_ROOT / "fixtures"
MANIFEST = FIXTURES / "manifest.json"
CONFIG_DIR = RUNTIME / "control"


def _write_config(name: str, payload: dict) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(label: str, *extra: str, expect_fail: bool = False,
         expect_kill: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-I", str(PROJECT_ROOT / "scripts" / "run.py"),
        "--manifest", str(MANIFEST),
        "--runtime", str(RUNTIME),
        "--transport", "script",
        "--fixture-root", str(FIXTURES),
        "--segment-size", "256",
        "--max-connections", "1",
        *extra,
        "download",
    ]
    print(f"\n=== {label} ===")
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True,
                            capture_output=True)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if expect_kill:
        assert result.returncode == -9, \
            f"{label} 应被 SIGKILL（-9），实际 {result.returncode}"
    elif expect_fail:
        assert result.returncode != 0, f"{label} 应当失败却成功了"
    else:
        assert result.returncode == 0, f"{label} 意外失败：{result.returncode}"
    return result


def _report(document_id: str) -> dict:
    cmd = [
        sys.executable, "-I", str(PROJECT_ROOT / "scripts" / "run.py"),
        "--manifest", str(MANIFEST),
        "--runtime", str(RUNTIME),
        "--transport", "script",
        "--fixture-root", str(FIXTURES),
        "report", "--document-id", document_id, "--json",
    ]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True,
                            capture_output=True, check=True)
    items = json.loads(result.stdout)
    return items[0]


def _checkpoint(document_id: str) -> dict:
    path = RUNTIME / "state" / document_id / "checkpoint.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _segments(document_id: str) -> list[Path]:
    directory = RUNTIME / "state" / document_id / "segments"
    return sorted(directory.glob("seg-*")) if directory.exists() else []


def _output(document_id: str) -> Path:
    return RUNTIME / "outputs" / f"{document_id}.pdf"


def main() -> int:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True)
    expected = {
        "A": "412c70927b4ded466acd65733d916f7ecf54362426c8f004381d6516d963835a",
        "B": "27fda23fc3e138b674a51d9c2986432740ca444919dfa4c094e36a590c6c7c7e",
    }

    # 阶段 1：写完第一个分段即被杀（检查点尚未记录），此时没有成品。
    _run("阶段1 新进程：写完分段后 SIGKILL",
         "--kill", "after_segment", "--kill-document", "A",
         expect_kill=True)
    assert not _output("A").exists(), "中断后不应存在成品文件"
    segs = _segments("A")
    assert len(segs) == 1, f"磁盘上应有 1 个已改名分段，实际 {len(segs)}"
    checkpoint = RUNTIME / "state" / "A" / "checkpoint.json"
    assert not checkpoint.exists(), "钩子在检查点之前触发，不应存在检查点"
    print(f"[断言] A 仅有孤儿分段 {[s.name for s in segs]}，无检查点、无成品")

    # 阶段 2：新进程先审计隔离孤儿分段，再续传，并在记录检查点后被杀。
    _run("阶段2 新进程：恢复（隔离孤儿分段），记录检查点后 SIGKILL",
         "--kill", "after_checkpoint", "--kill-document", "A",
         expect_kill=True)
    assert not _output("A").exists()
    quarantined = list((RUNTIME / "state" / "A" / "quarantine").glob("orphan-*"))
    assert quarantined, "阶段1 的孤儿分段必须在启动审计时被隔离"
    cp = _checkpoint("A")
    assert cp["segments"] and cp["mode"] == "range"
    print(f"[断言] 孤儿分段已隔离：{[q.name for q in quarantined]}")
    print(f"[断言] A 已验证区间 {[(s['start'], s['end']) for s in cp['segments']]}")

    # 阶段 3：新进程在发布前被杀；暂存文件必须被清理，成品不存在。
    _run("阶段3 新进程：补齐分段，发布前 SIGKILL",
         "--kill", "before_publish", "--kill-document", "A",
         expect_kill=True)
    assert not _output("A").exists(), "发布前被杀，成品不得存在"
    staging = list((RUNTIME / "outputs").glob(".*.staging"))
    assert not staging, f"暂存文件残留：{staging}"
    print("[断言] 无成品、无暂存残留，分段与检查点完整")

    # 阶段 4：刷新失败只阻塞 B；A 在此进程内完成发布。
    config = _write_config("refresh-fail.json",
                           {"behaviors": {}, "refresh_fail_ids": ["B"]})
    # 先让 B 的地址过期，触发刷新路径并失败。
    expired = _write_config(
        "expired-b.json",
        {"behaviors": {"B": [{"behavior": "expired", "times": 1, "then": "normal"}]},
         "refresh_fail_ids": ["B"]})
    _run("阶段4 新进程：B 地址过期且刷新失败，A 正常完成",
         "--script-config", str(expired), expect_fail=True)
    report_b = _report("B")
    report_a = _report("A")
    assert report_b["status"] == "failed" and report_b["failure_reason"]
    assert report_a["status"] == "done" and _output("A").exists()
    print(f"[断言] B 被单独阻塞：{report_b['failure_reason']}")
    print("[断言] A 不受影响，已发布成品")

    # 阶段 5：恢复 B（刷新入口恢复可用），全部完成后做独立校验。
    _run("阶段5 新进程：B 刷新恢复，完成全部下载",
         "--script-config", str(config))
    for document_id, digest in expected.items():
        output = _output(document_id)
        assert output.exists(), f"{document_id} 成品缺失"
        data = output.read_bytes()
        import hashlib
        actual = hashlib.sha256(data).hexdigest()
        assert actual == digest, f"{document_id} 哈希不符"
        assert data[:5] == b"%PDF-", f"{document_id} 魔数不符"
        assert len(data) == 687
        print(f"[断言] {document_id} 长度=687 魔数=%PDF- "
              f"SHA-256={actual}（与清单一致）")

    # 阶段 6：篡改 A 的起始分段（覆盖字节 0-255），新进程必须发现并隔离重下。
    first_name = _checkpoint("A")["segments"][0]["file"]
    target = RUNTIME / "state" / "A" / "segments" / first_name
    assert target.name.startswith("seg-000000000000-")
    tampered = bytearray(target.read_bytes())
    tampered[10] ^= 0xFF
    target.write_bytes(bytes(tampered))
    print(f"[注入] 篡改起始分段 {target.name} 第 10 字节")
    _run("阶段6 新进程：审计发现篡改，隔离分段并重新下载")
    report_a = _report("A")
    assert report_a["status"] == "done"
    assert any("tampered" in note for note in report_a["audit_notes"]), \
        f"审计未记录篡改：{report_a['audit_notes']}"
    quarantine = list((RUNTIME / "state" / "A" / "quarantine").glob("tampered-*"))
    assert quarantine, "篡改分段未进入隔离区"
    import hashlib
    assert hashlib.sha256(_output("A").read_bytes()).hexdigest() == expected["A"]
    print(f"[断言] 篡改被审计捕获：{report_a['audit_notes']}")
    print("[断言] 重下后的成品 SHA-256 仍与清单一致")

    print("\n全部演示阶段通过。")
    print(f"运行目录（含检查点、隔离区、成品）：{RUNTIME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
