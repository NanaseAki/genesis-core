# 架构

状态：M1、M2-A 和 M2-B1 已完成内部合成验收与合并。发行准备工具属于开发工具，不改变运行时模块、SQLite v1 或 Day 0；完整备份恢复、真实 LLM 验收和自动成长仍未实现。

## 形态与依赖方向

Python 模块化单体，Windows 为主，核心只使用标准库。M1 内存路径保留；持久化通过可选 `ConversationStore` 协议注入，不让 Core 导入 SQLite 实现。

```mermaid
flowchart LR
    Adapter[交互适配层/宿主] --> Core[Core]
    Adapter --> SQLite[SQLiteStore]
    Core --> Store[ConversationStore 协议]
    SQLite -.实现.-> Store
    Core --> Models[模型契约]
    Core --> Policy[权限契约]
    Fake[FakeModel] -.实现.-> Models
    Ollama[回环 HTTP OllamaModel] -.实现.-> Models
    SQLite --> Identity[身份契约]
    SQLite --> Memory[来源/证据契约]
    Growth[成长候选契约] --> Memory
```

| 边界 | 当前职责 | 后续职责 |
| --- | --- | --- |
| Core | 模型/策略编排，保留内存路径，可选存储协议；不可变会话/轮次 | 上下文预算、取消等 |
| Models | 同步不可变消息、完整回复、ModelError、FakeModel、非流式回环 OllamaModel/socket 超时 | 流式、取消、总时限、能力声明 |
| Storage | 稳定开发身份、Day 0、会话、输入、尝试、事件和成功轮次；短事务 | schema 迁移、完整恢复工具另行实现 |
| Memory | 观察/推断类型、来源引用、可信登记快照、MemoryStore 协议 | 记忆记录存储、检索、修订、删除 |
| Growth | 候选及提议协议，核验独立证据根 | 人工审阅与版本化决定；无自动算法 |
| Policy | 默认仅允许 RESPOND；无授权修改器 | 校验动作参数、目标与预算 |
| Adapters | 内存/持久化合成演示、默认自建合成 HTTP 的模型演示 | 桌面、语音、Live2D、游戏 |

AST 守卫覆盖 import/from-import/别名/相对导入及违规反例；领域层与模型不能依赖存储实现，Core 只引用协议，具体实现由宿主组合。静态检查和不可变对象不是针对恶意 Python 插件的安全沙箱。

## 模型与身份

`Model.generate(ModelRequest) -> ModelReply` 保持 M1 接口。请求只含伙伴/会话/输入 UUID 及不可变消息，不含数据库、策略、成长或登记表。`model_id` 是诊断信息，伙伴 UUID 不从它、设备或显卡生成。

`GenesisCore(identity, model, policy=None)` 的旧调用仍可用。新路径为 `GenesisCore(store.identity, model, store=store)`，身份不匹配明确拒绝。SQLite 的首次初始化随机生成 UUID，之后从已有数据库加载；当前 Day 0 强制标记 `synthetic_development`，没有真实伙伴正式启用界面。

FakeModel 保持确定性回显/脚本响应和耗尽失败，不联网、不下载模型、没有云端回退。预期模型错误保留因果链并包装为 TurnGenerationError；意外异常及 KeyboardInterrupt 传播。数据库只记录有限错误码，不存错误正文。

OllamaModel 使用同一协议和已有文本校验，模型名精确匹配、完整 stop 响应才能成功。只发送消息与生成参数，不发送 UUID 或状态句柄。宿主组合 HTTP/Store，Core 和 SQLite v1 均未更改；适配器失败仍记 model_error。socket 超时不是总时限，回环不证明服务端本地推理；具体安全、失败及未来流式/取消边界见 [OLLAMA](OLLAMA.md)。

共享 text 契约要求正文为非空白、无 NUL、可严格编码 UTF-8 的字符串。模型标识在 Core 调用模型前和存储提交入口双重校验，拒绝整数、布尔值、None、空串等，不允许 SQLite 隐式类型转换。模型回复验证失败属于契约错误，不能发布成功轮次。文本不裁剪、不替换、不归一化；具体兼容性见 PERSISTENCE。

## 会话与提交

M1 内存模式仍用 `_sessions[session_id] = new_session` 单次发布，原 57 项测试保留。它是单线程、无重入、无持久化模式。

SQLite 模式：

1. 读取成功回执。相同 `(session_id,input_id)`/内容重放，冲突明确拒绝；未知会话不自动创建。
2. 对新生成校验 RESPOND。拒绝时不接收输入或创建尝试。
3. `BEGIN IMMEDIATE`：绑定输入 ID/原文，记录宿主用户事件、尝试及基准会话版本，读取一致的历史快照，COMMIT。
4. **没有数据库事务时**执行模型。只有完整成功轮次进入历史上下文，失败用户输入仍在账本中。
5. 短事务校验尝试及版本，插入模型事件、唯一 turns 行，更新尝试与会话版本，COMMIT。turns 本身兼任回执，不维护可独立失配的成功去重表。

详细表结构、状态和重试规则见 [PERSISTENCE](PERSISTENCE.md)。模型执行不保证恰好一次；已提交结果具有幂等重放语义。输入的空格和 Unicode 原样保留。

兼容性：持久化输入一旦接收即绑定内容，模型失败也不能用同 ID 改文；内存模式未更改。跨重启重放保证值相等，不保证 Python 对象身份相等。稳定会话可通过 `store.session_ids()` 发现。公开返回值均为冻结数据类、元组或字符串，不泄漏连接/可变映射。

SQLite 连接属于一个线程；多个连接/进程由 SQLite 事务、唯一约束和会话版本防止错误提交，不依靠 Python 内存锁。竞争请求可能得到 StaleAttemptError，必须显式重试，不自动在旧上下文上提交。长会话未截断，每轮读取完整上下文；没有负载/容量基准。

事务入口从无活动事务状态建立所有权，嵌套调用明确拒绝且不接管已有事务；BEGIN 位于异常保护内，刚开启后发生 KeyboardInterrupt/SystemExit 也回滚本入口拥有的活动事务。COMMIT 已完成时连接不再处于事务中，不反向回滚；返回丢失仍通过持久回执恢复。

## 证据与权限

原始用户输入由宿主登记为用户根；完整模型回复始终是模型根。`record_derived` 只接受宿主选择的已存同会话来源，继承数据库中的根；工具回显复制原文，摘要/推断固定为 model_output，没有可提交 origin/root 的参数。SQL CHECK、外键和派生来源触发器进一步约束关系。

`evidence_registry(session_id)` 从宿主事件账本生成 M1 的 TrustedEvidenceRegistry。提议引用必须与登记完整匹配，可信根也必须存在并指向自身。模型根经工具回显不成为独立用户证据，用户根经摘要保留原归属；缺失或伪造来源不计入。来源核验不证明语义、事实或偏好成立。

记录摘要/推断是宿主 API，不是让模型自主选择可信根的工具。M2-A 无外部工具根采集器、记忆检索或成长状态更新。模型自己的重复回复不会触发人格变化。存储实现不接收模型自报的 verified 标记。

默认 Policy 仍只允许回复。宿主选择的会话账本写入是 RESPOND 流程的内部持久化，不是给模型授予 WRITE_MEMORY/APPLY_GROWTH/外部动作权限。当前没有成长或外部动作执行器。

## 数据与未验证事项

数据根在仓库外，默认 Windows LOCALAPPDATA/Genesis，可显式配置。现有损坏、未知 schema 或不一致 Day 0 明确报错，不重建或覆盖。打开已有文件的 SQLite 日志恢复是数据库自身的一致性恢复，不等于从备份恢复。

Day 0 包含规范初始状态、环境版本和 SHA-256；哈希不提供身份认证或可信时间证明。完整备份/恢复、隐私删除、加密、schema 升级均未实现；不能把数据库文件的普通复制宣传为一致备份。

已验证范围是合成本地数据库与独立进程中断；真实磁盘断电、磁盘耗尽、硬件故障、长期规模、多客户端负载、真实模型及 GPU 性能尚未验证。详见 [DATA_PORTABILITY](DATA_PORTABILITY.md)。
