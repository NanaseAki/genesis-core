# M2-B1：非流式 Ollama HTTP 契约

状态：标准库适配器已实现，通过自行启动的回环合成服务测试；**未安装或调用真实 Ollama/LLM，未验证纯本地推理、质量或 GPU 性能**。FakeModel 和 Model 协议保持兼容。无新增运行时依赖。

## 请求与成功条件

宿主显式注入 `OllamaModel(endpoint, model, options=GenerationOptions(...))`，Core 不导入具体模型。请求只发送 model、完整成功消息历史加当前 user 消息、`stream=false` 和明确的 options（temperature、seed、num_predict，默认 0/0/256）；不发送伙伴/会话/input UUID、数据库句柄或权限。未实现上下文截断/分词预算；长历史、请求体内存和上下文上限仍需后续验收。

只调用 `POST /api/chat`，不调用 pull/tags/show、自动发现、登录、下载或重试。参考 [Ollama 官方 Chat API](https://docs.ollama.com/api/chat) 和 [错误协议](https://docs.ollama.com/api/errors)。

成功必须满足 HTTP 200、application/json、无压缩、合法完整 UTF-8 JSON 对象。拒绝重复 JSON 键、NaN/Infinity、错误信封、非法 framing 和截断 body。默认 body 上限 1 MiB，可显式配置 1 字节至 16 MiB；Content-Length、chunked、关闭连接定界的三种正文均支持。普通非流式响应的计时/计数字段及 JSON 末尾换行保持兼容，不新增业务字段。

请求明确发送 `Connection: close`。每次只做一次请求，完整 framing 之后仍须读到 EOF，拒绝终止块/声明正文后的额外字节。服务必须遵守关闭请求；忽略它并保持连接的服务会得到 read_timeout，不能仅凭已收到 JSON 前缀提前成功。该取舍不禁用常用 chunked 或 Content-Length，不复用持久 HTTP 连接。

正文解析从 CPython HTTPResponse 的底层 fp 有界读取，不调用其宽松的 chunked 解码器；已取得的 response 使用独立上下文关闭，外层再关闭 connection，不依赖 GC。当前实测目标为 CPython 3.12/3.13，其他解释器需另行验收。

- chunk 长度为 1–16 位无符号 ASCII 十六进制；先与剩余正文预算比较，再读数据，负数/加号/0x 前缀均拒绝。每块强制 CRLF，零块后强制空行及 EOF。
- 支持经完整语法核验的 token 扩展及 ASCII 可打印 quoted-string 扩展（含可打印字符转义），作为无业务语义的 framing 元数据，不执行、不持久化。非法语法、obs-text 等不支持形式拒绝。
- 单条 chunk 控制行最多 4096 字节（含 CRLF），累计控制数据最多 65536 字节；控制预算独立于解码正文预算。声明 Trailer 的头及未声明的实际 trailer 均拒绝，不静默丢弃。
- 标准库标记的畸形响应头拒绝，避免非法字段被丢弃后误退化为关闭连接定界。重复 Content-Length/Transfer-Encoding、TE 与 CL 并存及非单一 chunked 编码均拒绝。Content-Length 只接受去除周围 SP/HTAB 后的 1–20 位 ASCII 十进制；不接受符号或多个值。
- 任何单次正文 read 最多 65536 字节；正文累计缓冲不超过配置上限。关闭连接定界时仅允许最多一字节超限探针，framing 完成后的 EOF 检查也只读一字节。临时块和返回 bytes 的拷贝仍会占用有界的额外内存，不宣称峰值内存等于正文大小。

响应头的行长/数量仍由标准库自身限制；正文和 chunk 控制限额不是全部 HTTP 头的合计上限。没有新增第三方网络依赖。

响应 model 必须是合法非空白字符串，且与配置**逐字相同**。不自动添加 `:latest` 或接受别名；用户应在后续真实验收中明确配置服务实际返回的规范模型名。身份不因模型变化而重建。

`done` 必须是布尔 true，`done_reason` 必须为 stop。length、缺失原因或未完成标记均拒绝；这是本版比通用 Ollama 更保守的发布规则。message.role 必须为 assistant，content 通过原有 ModelReply 文本校验（非空白、无 NUL、严格 UTF-8），不修剪/替换/归一化。工具调用不支持；非空或非法 tool_calls 拒绝。thinking 与其他诊断字段不发布/持久化，不执行工具，也不将模型声明的 origin/root 当作可信来源。

## 客户端网络边界

只接受显式回环 IP 的 HTTP origin 和 1–5 位十进制端口（数值 1–65535）；IPv6 必须使用方括号且闭括号后只能是冒号及端口。允许 IPv4 127/8、IPv6 ::1，以及标准库识别为 loopback 的 IPv4 映射 IPv6；不接受 localhost、DNS 名、远程 IP、HTTPS、用户信息、额外路径、query、fragment 或 zone ID。使用显式 authority 语法校验，不依赖 urlsplit 补丁版本的宽松/严格差异。HTTPConnection 直接连接，不读取环境代理、不跟随 Location，没有备用 endpoint。

这保证本适配器只连接配置的回环服务，**不能证明服务不转发云端**。真实验收前须独立核实云功能禁用、服务进程实际配置、所选模型运行位置及服务端外连。官方 [FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features) 说明 `OLLAMA_NO_CLOUD=1` 或 server.json 中 `disable_ollama_cloud=true`，修改后须重启并核对日志。此处只是验收要求，本轮未读取/修改用户 Ollama 配置或启动服务。HTTP 成功、返回本地地址或模型自报名称均不是纯本地推理证明。

## 超时、错误与持久化

connect_timeout 默认 5 秒，用于建立连接；连接建立后 read_timeout 默认 60 秒，用于 socket 发送、响应头和读正文的等待。两者均为正有限数值。**没有严格端到端总时限**：逐步到达的数据和多次 socket 操作可使总耗时超过 read_timeout，慢速持续输出不保证在固定墙钟时间停止。没有自动重试。

`OllamaError(ModelError)` 提供脱敏 reason 和可选 HTTP status。公开原因链保留安全的 OllamaDiagnostic 分类，不保留 BadStatusLine 原始行、JSONDecodeError.doc、IncompleteRead.partial 等正文载体。超时采用新建的 `TimeoutError(reason.value)` 安全快照，仅保留必要的超时类别及 connect_timeout/read_timeout 原因码；不复制或引用原始异常的 args、OSError 属性、附加字段、notes、traceback、cause 或 context。

兼容性变化：公开 cause 不再与原始 TimeoutError 是同一对象。适配器不再修改共享原异常，也不会使先前发布的诊断随其他调用变化。适配器不写原始诊断属性、cause/context 或 traceback；Python 自身抛出原异常时追加栈帧、关联环境 context 的行为不在它控制范围。直接创建快照的单元测试还验证原 traceback 对象身份不变。调用者应读取 OllamaError.reason 和安全原因类别，不依赖原始异常身份或原文。

预期底层错误在退出 except 后才抛出公开异常；抛出后清除 Python 自动附加的环境 context，再原样重新抛出。测试遍历公开 cause/context 图及其文本属性，并检查默认格式化 traceback，Core 的 TurnGenerationError 包装路径也不得泄露合成敏感标记。不是只使用 from None。未吞掉 KeyboardInterrupt/SystemExit 或意外程序错误。

保证范围是预期模型错误的公开异常消息、数据属性和默认 traceback；调用栈本身不可避免接触输入，主动 capture_locals、调试器和内存转储不在脱敏保证内，不应将它们作为生产日志。适配器没有原始响应日志，数据库继续只保存有限错误码。原因如下：

| reason | 含义 |
| --- | --- |
| connection_refused | 操作系统明确拒绝连接 |
| connect_timeout / read_timeout | 建连/后续 socket 操作等待超时 |
| connection_error | 断连、协议连接错误等；服务端执行结果可能未知 |
| http_error / server_error | 非 200 状态（5xx 为 server_error）或 200 错误信封 |
| redirect_rejected | 3xx，绝不跟随 |
| invalid_json / invalid_response | JSON 编码或结构/HTTP framing 契约错误 |
| model_mismatch | 响应模型名与配置不一致 |
| invalid_text | 回复违反现有文本契约 |
| incomplete_response | 未完成/截断或非 stop 结束 |
| response_too_large | body 超过配置上限 |

Core 将预期适配器错误包装为 TurnGenerationError，cause 保留 OllamaError；SQLite v1 仍为 failed/model_error，未新增状态或错误码。该 failed 表示“无已发布轮次”，**不表示服务端没有执行**。完整回复仅在既有同一 SQLite 事务中发布，HTTP/模型执行期间无数据库事务。

KeyboardInterrupt/SystemExit 在连接关闭后继续传播，Core 按既有 interrupted/generation_interrupted 记账；不声称服务端生成已取消。进程强制退出可能留下 running，表示执行未确认。重试由调用者显式决定：同 input ID 必须原文相同；显式重试隔离旧 running 为 unknown；stale_context 需重新取得上下文显式调用；成功提交直接重放，包括重启或模型服务已关闭。生成不保证恰好一次。

## 演示和验证

```powershell
py -3.12 -B -I -S tools/check_loopback_tests.py
py -3.13 -B -I -S tools/check_loopback_tests.py
py -3.12 -B -I -S tools/run_ollama_demo.py
# 安装后
.\.venv\Scripts\python.exe -I tools/check_loopback_tests.py --installed
.\.venv\Scripts\python.exe -I tools/check_persistence_entry.py
.\.venv\Scripts\genesis-ollama-demo.exe
```

默认 demo 自建随机端口合成服务，固定中文输入、临时合成数据库、重开重放并校验一次 POST。console 以 ASCII JSON 转义显示回复以兼容 Windows 重定向代码页；HTTP/数据库原文不变。真实服务模式只有在同时指定 `--real-service --endpoint <origin> --model <exact-name>` 时启用；本轮不运行。即使该模式也只用固定合成输入和临时合成身份，无正式伙伴启用功能。

测试覆盖真实回环 HTTP、代理陷阱、重定向陷阱、连接失败、事件控制超时、协议负例、事务与重放。Windows spawn 子进程在服务停止后重放成功，包含超时、错误报告及 terminate/kill/join 清理；检查安装模式子进程仍使用同一个包。测试主进程的 socket 审计拒绝外部 IP 和名字解析，HTTP 子进程有独立 guard。它不是 OS 级防火墙，不证明其他程序或未监控子进程没有外连；其余项目子进程仅为本地 Git/SQLite/FakeModel。官方文档查阅属于开发调研，不属于测试联网。

## 尚未实现及真实基准前置

- 没有流式、显式取消、严格总时限、工具执行、后台重试、上下文预算或真实模型质量验收。
- SQLite v1 只保存现有 model_id 和回复，不新增服务版本、权重摘要、生成参数或计时指标字段；真实基准须另行记录配置，不能仅凭 model_id 声称推理可复现。
- 未来流式片段仅是临时显示，不能逐段提交成功历史/成长证据；最终完整验证后才可进入既有原子提交。取消的服务端确认和取消/提交竞争需要先设计测试，不能新增未经审查的 v1 状态。
- 真实基准须另行授权模型与服务，记录服务/模型版本、量化、上下文、参数、云功能状态、实际 CPU/GPU 位置；只用合成提示、临时身份，禁用其他工作负载后比较冷/热请求总延迟、内存/显存、超时/断连。非流式 API 不能直接测量首 token 延迟；流式基准另行实现。
- 未实现完整备份/恢复、schema 迁移、隐私删除和自动成长；本轮持久化格式与 Day 0 未改动。
