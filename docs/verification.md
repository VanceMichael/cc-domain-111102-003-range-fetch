# 成品文件验证指南（给操作人员）

文件名存在不代表文件正确。本组件发布的任何成品都必须通过下面的检查；任何
一项不通过，都应把该文档视为未交付，并保留 `runtime/.../quarantine/` 中的物证。

## 1. 从报告确认 document_id 的状态

```sh
python3 -I scripts/run.py --manifest fixtures/manifest.json \
    --runtime runtime/dl report --document-id A
```

关键字段含义：

| 字段 | 正确取值 |
| --- | --- |
| `状态` | `done` 才表示已发布；`failed`/`new`/`range`/`full_only` 都是未完成 |
| `实体 ETag` | 续传所依据的强实体校验器；显示“无强校验器”时该文档不可跨进程续传 |
| `可跨进程续传` | `是`=基于强 ETag；`否`=中断后全量重下的保守模式 |
| `总长度` | 实体字节数，成品长度必须与之相等 |
| `已验证字节范围` | 闭区间列表，必须从 0 开始、首尾相接、最后一段结束于 `总长度-1` |
| `已验证字节数` | 各区间长度之和，必须等于 `总长度` |
| `成品 SHA-256` | 对成品流式计算的实际摘要 |
| `清单 SHA-256` | 清单声明的期望摘要 |
| `完整性结论` | 必须出现“已通过长度与清单 SHA-256 校验” |
| `失败原因` | 未完成时的最后一次错误；完成时为空 |
| `启动审计` | 隔离区物证（孤儿、篡改、无强校验器等），仅作历史记录 |

需要机器处理时加 `--json`。

## 2. 独立复核（不依赖本组件的输出）

在项目根之外、拿到成品文件后，用系统工具独立验证：

```sh
FILE=runtime/dl/outputs/A.pdf

# (1) 长度：与报告 total_length 严格相等
stat -c '%s' "$FILE"

# (2) 摘要：与清单 sha256 严格相等（下面用 fixtures/manifest.json 中的值示例）
echo "412c70927b4ded466acd65733d916f7ecf54362426c8f004381d6516d963835a  $FILE" \
    | sha256sum -c -
# 期望输出：runtime/dl/outputs/A.pdf: OK

# (3) 魔数：PDF 必须以 %PDF- 开头，PNG/ZIP 等类型同理对照签名
head -c 5 "$FILE" | od -An -tx1   # PDF: 25 50 44 46 2d
```

也可以用 Python 独立计算，避免信任 shell 别名：

```sh
python3 -I -c '
import hashlib, sys
p = sys.argv[1]
h = hashlib.sha256()
with open(p, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 16), b""):
        h.update(chunk)
print(h.hexdigest())
' "$FILE"
```

## 3. 清单未提供 SHA-256 时

此时无法独立证明实体真伪，报告的完整性结论会明确提示
“清单未提供 SHA-256：仅验证长度与分段哈希链”。可以做到的保证：

- 每个分段都有独立 SHA-256 记录在 `checkpoint.json`，恢复时逐段复核；
- 成品长度等于实体总长度、分段连续无重叠；
- 媒体魔数与声明类型一致、HTML 登录页被拒绝。

建议向来源方索取官方摘要后补入清单再跑一次；在拿到摘要前，不要把该文件
当作可对外交付的定稿。

## 4. 失败与中断后的判定

- `outputs/` 下没有对应文件 = 未交付（组件保证失败时不残留成品）；
- `state/<id>/checkpoint.json` 的 `mode` 为 `failed` 时，`last_error` 是原因；
- 重新执行同一条 `download` 命令即可从已有分段恢复，已验证字节不会重下；
- `quarantine/` 中出现 `tampered-*` 说明本地字节曾被改动，应排查存储与权限；
- 地址刷新失败只阻塞对应 document_id，其他文档的成品与进度不受影响。

## 5. 崩溃恢复演示

`python3 -I scripts/run_demo.py` 会在全新进程中分别于
**写完分段后、记录检查点后、发布文件前** 杀死进程，再启动新进程验证恢复，
并额外演示刷新失败隔离与分段篡改检测。断言全部通过时末尾输出
“全部演示阶段通过”。
