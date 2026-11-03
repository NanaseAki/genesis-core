# M2-A 持久化契约

状态：本地实现、合成数据验收；M1 内存路径保留。此文说明 SQLite v1，不是完整事件溯源框架或备份格式。

## 初始化与版本

`SQLiteStore(data_root=None)` 是显式初始化入口，无导入副作用。数据根优先级为函数参数、GENESIS_DATA_ROOT、平台用户目录。Windows 默认 `%LOCALAPPDATA%\Genesis`；macOS 为用户 Library/Application Support/Genesis；其他平台为 XDG_DATA_HOME/Genesis 或用户 .local/share/Genesis。显式路径必须绝对，检查规范化原路径和解析链接后的路径及其祖先，拒绝普通 Git 工作区、常规裸仓库及源码包内部位置。

普通工作区识别 .git 目录/文件；裸仓库要求 HEAD、objects、refs 或 packed-refs、config 的组合，并核对 HEAD 格式与本地 core.bare。段名和键名大小写不敏感，按出现顺序处理重复段/键；后值覆盖前值，但非法前值同 Git 一样报错。支持布尔别名、无值项、分段引号（如 f"al"se）、Git 的双引号/反斜杠/n/t/b 转义、反斜杠续行、引号外注释和有符号 32 位数值布尔项（八/十六进制、k/m/g 后缀）。非零为真，零为假。无值项后直接加注释是非法配置，不当作 true；保留读取时的换行字节语义，以 LF/CRLF 分行，不把注释中的孤立 CR 或其他 Unicode 分隔字符变成新配置项。

只检查常规本地元数据，不调用 Git、不读取外部路径或 Git 环境配置。遇到 include/includeIf、缺失 core.bare、字面多行引号值、不支持/非法语法或超过 1 Mi 字符的 config，保守拒绝数据根；不宣称它们一定是裸仓库。即使条件 include 实际未生效也不自动放行，需要用户选择无歧义的仓库外目录。不会因单独名为 HEAD/objects/refs 就判定仓库。这是防误配置检查，不是通用 Git 解析器或恶意文件系统攻击沙箱。原路径位于仓库内但通过链接指向外部也拒绝，外部链接指入仓库同样拒绝。

决策 token（布尔值、段名、键名）只接受相应 ASCII 语法；大小写处理仅用 A–Z 到 a–z 的显式映射，不使用 Unicode casefold/lower。falſe、0K、-0K 等非 ASCII 布尔值必须在转换前拒绝，不能被折叠成 false/零。此限制不施加到整个文件：合法 Unicode 注释、引用子节及不参与 bare 决策的文本值仍可存在。合法 ASCII 数值、单位后缀、引号、转义、续行和有序覆盖规则不变。

M2-A 文件名固定 `genesis-synthetic.sqlite3`，Day 0 标记 `synthetic_development`；没有真实伙伴正式启用入口。路径防护防止误放，不抵抗恶意并发替换链接。不要将活跃数据库放在云同步目录或网络文件系统。

数据库使用 SQLite 3.37+ STRICT 表、foreign_keys=ON、synchronous=FULL、默认回滚日志；锁等待上限 5 秒。每条连接显式开启外键。模型执行前事务结束，连接不长期持有读/写事务。

文件以排他方式首次创建；整个 schema、身份与 Day 0 在一事务内初始化。已有文件即使是空文件也不视为新数据，不自动重建。初始化中断留下的未完成文件明确拒绝，需要人工检查，当前没有修复工具。并发首次初始化可能使其中一个打开者看到未完成文件并失败，须等初始化者结束后显式重试，不会生成两个已提交身份。

`PRAGMA application_id=0x47454E31`、`user_version=1`；Day 0 `format_version=1` 独立标记。打开时校验版本、完整性、外键、精确 v1 schema、Day 0 和跨表账本关系。不支持旧/新版本、外部改动或损坏时明确报错并保留文件，不做隐式升级或降级。SQLite Error 原样传播；验证问题用 SchemaVersionError/DataIntegrityError/Day0ValidationError。

## 最小 schema

| 表/索引 | 内容与约束 |
| --- | --- |
| companion | 单行 singleton=1；随机伙伴 UUID、UTC 创建时间、唯一 Day 0 事件和摘要 |
| sessions | 会话 UUID、伙伴外键、创建时间、提交 revision |
| events | 全局递增 sequence、唯一事件 UUID、伙伴/会话外键、kind/origin、根与直接父事件、原文、宿主 UTC 记录时间 |
| one_day0 | day0 kind 的唯一部分索引，防止重复 Day 0 |
| requests | 主键 `(session_id,input_id)`；唯一用户事件外键，原始输入从事件读取 |
| attempts | 每次调用尝试 UUID、请求外键、基准 revision、状态、有限错误码、起止时间 |
| turns | 主键 `(session_id,input_id)`、唯一会话 position、唯一模型事件及成功 attempt 外键、model_id；同时是历史和成功回执 |
| derived_provenance | 插入派生事件时检查父事件/根关系与同会话归属；不能把模型根换成用户根 |

事件只由明确宿主入口创建。kind 分 day0/user_input/model_reply/tool_echo/summary/inference；origin 分 host_initialization/user_input/model_output/tool_result。模型原始回复以自身为根，不借用户输入的根提升证据。摘要/推断始终是 model_output，但保留宿主选择的原始根；M2-A 没有原生外部工具根采集器。

request/input UUID 与 event UUID 是不同标识；同 input UUID 可在不同会话出现。sequence 提供记录顺序，turn position 提供会话成功历史顺序，二者不靠墙钟排序。recorded_utc 是宿主接收/记录时间，不宣称等于外部实际发生时间；外部事件采集协议尚未实现。

所有表通过唯一 singleton 伙伴谱系关联；没有额外设备指纹或模型派生身份。外部 SQL 写入者和模型适配器是可信代码边界，不声称数据库能抵抗持有写权限的攻击者。

## 三个事务边界

- 初始化：schema、companion、Day 0 一次提交，既有身份只读加载与验证。
- 接收尝试：`BEGIN IMMEDIATE`，按请求核对原文，必要时插入用户事件/请求；将同输入的旧 running 尝试置为 unknown，保存新 running 尝试及一致历史快照；COMMIT。
- 成功发布：`BEGIN IMMEDIATE`，重查成功回执、尝试状态和会话 revision；原子插入模型事件/turns、更新尝试 committed 和 revision；COMMIT。失败记录也使用独立短事务。

事务管理器对 Exception/KeyboardInterrupt/SystemExit 在仍有活动事务时回滚并重新抛出。COMMIT 已完成后的中断不反向伪装成失败。历史与回执是同一 turns 行，数据库提交后才返回结果，无内存缓存需要同步。模型不在任何这些事务中运行。

事务所有权在入口处通过无活动事务状态建立。嵌套入口抛 TransactionNestingError，不提交或回滚已有事务。BEGIN 自身位于 try 保护内，即使 SQLite 已接受 BEGIN、Python 尚未返回或刚返回时中断，也会清理本入口的活动事务。锁等待仍为 5 秒，未通过增加等待时间掩盖缺陷。

并发相同输入：重试为旧 running 尝试设置 unknown，相当于隔离其提交资格；旧执行可能仍在运行，不称为确认崩溃。若新结果已提交，旧执行返回数据库中的胜出结果；否则旧执行提交报 StaleAttemptError。不同输入并发生成：只允许仍基于当前 revision 的结果提交，失败者记录 stale_context，显式重试后重新取得上下文。模型不保证执行恰好一次，成功持久化结果至多一份。

## 故障与重试矩阵

| 位置/结果 | 持久状态与下一次动作 |
| --- | --- |
| 权限拒绝/未知会话 | 不创建请求、尝试或轮次，原异常传播 |
| 接收事务失败/中断且未提交 | 不留下半个用户事件或请求；数据库错误传播 |
| 已接收，预期模型失败 | 输入仍绑定，attempt=failed/model_error；历史不变，同 ID/内容可重试 |
| 模型意外异常 | attempt=failed/unexpected_model_error，原异常传播，不记录异常正文 |
| 可捕获的生成中断 | attempt=interrupted，KeyboardInterrupt/SystemExit 重新抛出 |
| 进程在生成期间退出 | attempt 仍为 running：表示执行结果未确认；不自动重放未完成工作，显式重试将旧尝试置为 unknown |
| 写入中途失败/COMMIT 前中断 | 模型事件、turn、成功尝试与 revision 同时回滚；此前用户事件/attempt 保留，结果可能已生成但未保存，重试可能再次执行模型 |
| COMMIT 成功，返回丢失/进程退出 | 已提交轮次完整可见，同 ID/内容重放，不重新调用模型 |
| 数据库在记录失败状态时再报错 | 数据库错误传播并保留原异常上下文；attempt 可能仍 running，不能谎报已记录失败 |
| 版本/损坏校验失败 | 拒绝打开，不删除、不覆盖、不自动重建 |

持久化请求接收后不能用同 ID 换内容，包括失败之后；M1 内存模式不改变。所有重试由调用者显式发起，没有后台自动重试。持久化 replay 保证值相等，不要求对象身份相等。

M2-B1 的 OllamaError.reason 区分连接拒绝、socket 超时、HTTP/JSON/契约失败，但 SQLite v1 不增加状态或错误码，仍为 failed/model_error。此处 failed 只说明没有发布成功轮次，不证明服务端未运行/已取消。连接断开或超时可能丢失已生成结果；不得自动重试。进程退出残留 running、显式重试隔离为 unknown、stale_context 及已提交重放规则保持不变，见 [OLLAMA](OLLAMA.md)。

## 文本与模型元数据契约

UserInput、Message、ModelReply、MemoryRecord、GrowthCandidate 的业务正文及宿主事件入口共享 validate_text：必须是非空白字符串、不得包含任何位置的 NUL，必须可严格编码 UTF-8（拒绝代理码点字符串）。合法中文、换行、组合字符及非 NUL 控制字符原样保留，不调用 strip 后保存、不替换字符、不做 Unicode 归一化。错误使用不包含原文的 TextContractError（ValueError 子类）。

model_id 使用同样的文本要求，Core 两条路径均在模型调用前检查；存储 commit_turn 再验证。整数、布尔值、None、字节串不能通过 str() 或 SQLite TEXT 亲和性变成合法标识。无效模型元数据抛 ModelError；模型回复文本契约失败也由 Core 包装为 TurnGenerationError，持久化尝试记为 failed/model_error，历史不变。事件/派生正文入口亦拒绝数值隐式转换。

本修复保持 v1 DDL 的物理 SQL 完全不变，schema_version、Day 0 格式均不变，不引入升级器。SQL 保留原来的最小非空约束；宿主入口在写入前执行完整文本规则，排除 NUL 后 SQL length 不会截断合法正文。拥有任意 SQL 写权限的代码仍属于可信边界，数据库不是文本过滤沙箱。

打开已有 v1 数据时还校验 events.content 与 turns.model_id。此前 SQL 可接受的嵌入 NUL 等违约记录会导致 DataIntegrityError，保留现有文件字节，不裁剪、不覆盖、不重建。原有符合契约的 v1 数据可不经迁移直接打开，并保留身份和 Day 0。

## Day 0

随机伙伴 UUID、一次 UTC 创建时间、schema/格式版本、合成数据标记、初始状态和必要环境版本组成规范 JSON。名字/兴趣/偏好为 null；个人经历、确认记忆、接受成长、外部授权和主动任务为空；权限只有 respond。环境记录 Genesis/Python/SQLite 版本、OS 家族和规则版本，不记录用户名、绝对路径、设备序列号、GPU 或模型名。

编码为 UTF-8，键排序，无额外空格/尾随换行，禁止 NaN；SHA-256 存在 companion 行，与 payload 分离。验证同时检查规范字节、摘要、版本、身份、UTC、空白状态和权限基线。重启保留最初元数据，不拿当前运行环境重写历史。

这里只实现数据库内生成和验证，没有独立快照导出包、签名、可信时间、加密或 Day 0 恢复工具。能同时改写 payload 和摘要的人可以伪造它，摘要只说明一致性。

## 已验证与限制

测试均为临时目录/合成内容。真实子进程分别完成初次提交、退出后重放、生成期间 os._exit、提交前 os._exit（活跃事务/回滚日志恢复）、提交后 os._exit；比较身份、Day 0 摘要、事件 ID、模型调用数与历史。另含写入触发器失败、COMMIT 前后 KeyboardInterrupt、两个连接竞争、外键/唯一约束、schema/Day 0 篡改、错误传播和来源反例。

新增 BEGIN 返回边界的精确中断注入、嵌套所有权与失败 BEGIN 测试。三种双进程竞争由 multiprocessing spawn 和双向管道 READY/release/complete 握手控制：相同输入旧尝试先返回、新尝试先提交、不同输入过期上下文拒绝；没有 sleep 排序。每步有超时，finally 关闭管道并 terminate/kill/join 残留子进程。安装后测试还确认子进程导入同一已安装包，而非偷偷注入 src。

已验证的是已提交本地数据跨重启的一致性与幂等性，以及这些模拟故障的回滚。不是硬件断电或介质损坏实测，不是完整备份恢复。没有实现 SQLite Backup API 导出、迁移器、隐私删除、数据库加密、跨设备同步或模型迁移测试。不要复制活跃主文件作为一致备份；正式数据启用前必须另行验收数据保护与恢复流程。

当前每轮读取完整成功历史，初始化进行全库完整性校验；长历史性能/容量、真实断电、磁盘耗尽、长锁竞争仍需实测。M2-B 前应审查失败输入 ID 绑定、unknown/stale 重试 UX、超时取消边界及可信模型适配器的权限范围，不以接入 LLM 代替持久化审查。
