# 采购附件的实体校验与分段续传

这个 Python 项目保存采集侧的来源读取工具和合成资料。代码位于 app，示例输入位于 fixtures，现有读取工具的回归位于 tests。示例不代表任何真实单位的业务记录。

## 数据与接口

AttachmentEndpoint 提供文件响应，不写入客户端状态。fetch 的 behavior 可选择地址过期、忽略 Range、登录页面、短响应及错误区间；refresh 返回新的合成位置。fixtures 中的 PDF 字节用于下载完整性研究，不依赖 PDF 渲染器。documents.json 中的身份与临时 URL 独立。

## 本地使用

使用 Python 3.12 或更新版本，当前工具仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.inspect
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
```

读取命令输出当前素材的结构或单次读取结果。运行过程中产生的文件放在 runtime 中，与原始素材分开。公开示例仅使用 .invalid 域名，网络适配时应替换为已授权的来源。
