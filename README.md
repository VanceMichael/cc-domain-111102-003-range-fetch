# 采购附件的实体校验与分段续传

这个 Python 项目保存采集侧的来源读取工具、可靠下载组件和合成资料。代码位于
`app`，命令行入口位于 `scripts`，示例输入位于 `fixtures`，测试位于 `tests`。
所有字节与响应元数据都是自造样本，不包含任何真实采购文件、密钥或选中片段；
公开示例仅使用 `.invalid` 域名，接入网络时必须替换为已授权来源。本组件只
采集公开或已授权附件，不自动注册、不提交投标。

## 模块构成

| 模块 | 职责 |
| --- | --- |
| `app/manifest.py` | 读取附件清单（document_id、媒体类型、可选 SHA-256、刷新入口、临时 URL） |
| `app/transport.py` | 传输抽象：`ScriptedTransport`（合成故障）与 `HttpTransport`（标准库 urllib） |
| `app/verify.py` | 强 ETag、Content-Range 严格解析、HTML 登录页识别、媒体魔数、流式 SHA-256 |
| `app/state.py` | 不可变分段、原子检查点、启动审计、隔离区、暂存与原子发布 |
| `app/engine.py` | Range/If-Range 续传状态机、故障处理、连接/内存约束、操作人员报告 |
| `app/cli.py` | `download` 与 `report` 两个子命令 |

## 核心安全规则

下载 URL 只是暂时位置，**文件身份由 document_id、强实体校验器和摘要决定**。
以下任何一种响应都不会被拼进已有分段：

- 服务端忽略 Range 返回 `200`（旧分段整体隔离，按新实体全量重下）；
- ETag 改变（旧分段属于不同实体，拒绝拼接）；
- `416`（分段不完整时按瞬时故障重试，仅在确已完整时视为越过 EOF）；
- 短响应（实际字节少于 Content-Range/Content-Length 声明，临时文件删除）；
- 错误的 Content-Range（起止与请求不符、越过总长度、与 Content-Length 矛盾）；
- 两个分段重叠（重叠内容拒绝写入，已验证字节原样保留）；
- HTML 登录页，即使状态码是 `200`（识别后刷新地址再试，绝不当作 PDF）。

只有**长度连续覆盖整个实体**且**摘要校验成立**（清单提供 SHA-256 时）的内容
才会原子发布为成品；失败时成品与暂存文件一律不存在，不会留下看似完整的文件。

## 无稳定校验器时的保守策略

HTTP 的 If-Range 接受强 ETag 或 Last-Modified，但后者只精确到秒，弱 ETag
（`W/` 前缀）按规范不支持 Range 语义。本组件**只承认强 ETag**：

- 能拿到强 ETag：分段可跨进程续传，请求统一携带 `If-Range: <etag>`；
- 源始终不给强 ETag：进入 `full_only` 模式。单进程内仍按固定大小切分不可变
  分段（每段独立 SHA-256，短响应立即暴露），但**进程退出后这些分段在下次
  启动时整体隔离并全量重下**。

代价：没有强校验器的源，每次中断都要重新下载整个文件；换来的保证是绝不把
无法证明属于同一实体的旧字节拼进新文件。报告的 `resumable` 字段会标明该文档
是否可跨进程续传。

## 本地使用

要求 Python 3.12+，仅依赖标准库。运行时一律使用 `-I`（隔离模式），阻止
PYTHONPATH、用户 site-packages 与脚本目录注入；项目根由启动器显式加入
`sys.path`，不依赖当前工作目录：

```sh
# 合成端点上的完整下载
python3 -I scripts/run.py --runtime runtime/dl download

# 按 document_id 查询下载清单（实体、已验证字节范围、失败原因）
python3 -I scripts/run.py --runtime runtime/dl report
python3 -I scripts/run.py --runtime runtime/dl report --document-id A --json

# 注入合成故障（短响应、登录页、忽略 Range、地址过期、刷新失败）
python3 -I scripts/run.py --runtime runtime/dl2 \
    --script-config fixtures/script-faults.json download

# 崩溃三阶段 + 刷新隔离 + 篡改检测的完整演示（每个阶段都是全新进程）
python3 -I scripts/run_demo.py

# 测试（隔离模式）与编译检查
python3 -I scripts/run_tests.py
python3 -m compileall -q app tests scripts
```

运行产生的所有文件都在 `runtime/`（已在 `.gitignore`）中，与原始素材分离。

## 运行目录结构

```
runtime/<name>/
  state/<document_id>/
    checkpoint.json                 原子替换的检查点（实体、分段哈希、模式、错误）
    segments/seg-<start>-<end>      只增不改的不可变分段
    quarantine/                     崩溃残留、孤儿、篡改、过期实体等隔离物证
  outputs/.<id>.staging             发布前暂存（失败即删除）
  outputs/<id>.pdf                  仅在校验全部通过后原子改名出现
```

## 如何验证最终文件（而不是只看文件名存在）

详见 [docs/verification.md](docs/verification.md)。最短版本：

```sh
# 1) 长度必须等于检查点/报告中的 total_length
stat -c '%s' runtime/dl/outputs/A.pdf

# 2) SHA-256 必须等于清单中的期望值
echo "412c7092...63835a  runtime/dl/outputs/A.pdf" | sha256sum -c -

# 3) 魔数必须与媒体类型一致（PDF 应以 %PDF- 开头）
head -c 5 runtime/dl/outputs/A.pdf   # 输出 %PDF-

# 4) 报告中的完整性结论必须是“已通过长度与清单 SHA-256 校验”
python3 -I scripts/run.py --runtime runtime/dl report --document-id A
```

## 接入真实（已授权）来源

```sh
python3 -I scripts/run.py --transport http \
    --manifest /secure/path/manifest.json --runtime /secure/path/runtime download
```

真实刷新入口默认按 `GET <refresh_entry>` → `{"url": "..."}` 的 JSON 契约实现
（见 `app/transport.py::http_refresh_json`），站点契约不同时替换该函数即可。
引擎只依赖 `Transport` 协议（`get` 带请求头、返回状态/头/只读流；`refresh`
返回新 URL），不需要改动任何校验与状态逻辑。
