# Project Genesis

长期成长型、本地优先的原创 AI 伙伴。成长历史与伙伴身份由 Genesis 管理，模型作为可替换的能力提供者。

初始不编造个人经历、固定兴趣或具体性格。偏好形成需要真实互动证据、修订记录和长期验证。

## 当前状态：独立 Core 私有准备

M1、M2-A、M2-B1、Open Source Preparation P1 和 P2-A 已验收合并。P2-A 历史基线通过 Windows Python 3.12/3.13 各 270 项源码与安装回归；R4 在受控发行边界内技术关闭。Apache-2.0、项目归属、贡献政策和许可证专项发行校验已实施。

原字节干净基线已按批准的修订验收合同通过 Python 3.12/3.13 本地限定验收，各 289 项完整测试（其中 6 项 replacement）。参考提交 `0696e2279df7ef87a95a2ba5a6934440cbe166c4` 已在新 Private 仓库通过双版本远端 CI，具体范围及首次根推送事件字段的缺口见下文“Windows CI 的提交范围”。当前文档修订基于该参考提交，仅完成静态核验，尚未对修改后文件集重新运行测试、构建或 CI；参考提交与历史基线结果均不构成本修订的动态验收，也不代表获准公开或发行。验收范围与证据限制见 [发行准备说明](docs/RELEASE_PREPARATION.md)。

所有者已确认 Apache-2.0、NanaseAki 与本人核实的 GitHub noreply、独立干净快照方案 B，以及优先 PVR。旧仓库按既定方案独立保留 Private，未来公共 Core 为唯一公开开发主线，不继承旧 Git/GitHub 附属历史。许可证实施不等于获准创建公共仓库、Public、Release 或 PyPI 发布；目标仓库及 PVR 状态须在相应阶段重新核实，本候选不声明公开入口或 PVR 已启用。项目独立开发，不代表 Neuro-sama、AIRI、Live2D 或 Ollama 官方。

**已实现**：

- Python `src` 包结构；运行时零第三方依赖。
- 稳定、由宿主明确提供的伙伴 UUID；独立于模型名称。
- 同步文本模型契约与确定性 `FakeModel`。
- 非流式 Ollama `/api/chat` 适配器：显式回环 IP、严格回复验证、有限错误分类；通过合成 HTTP 服务验收，未调用真实 LLM。
- 有界的 chunked framing/终止校验、独立响应流关闭、公开异常图脱敏及跨版本 authority 校验，已通过合成回归和限定复审。
- 内存会话、不可变快照、成功请求去重、显式失败与重试。
- 观察、推断、成长候选的不同类型与来源引用。
- 候选引用由宿主独立持有的可信事件登记表核验，按可信根去重；模型根经工具回显也不增加独立偏好证据。
- 默认仅允许回复的策略；固定合成数据演示；标准库测试。
- 可选 SQLite 存储：跨重启稳定 UUID、一次 Day 0、会话发现、交互事件、请求与尝试账本、已提交轮次重放。
- 事务和唯一约束保护持久化去重；推理期间没有数据库事务；过期尝试不能覆盖已提交历史。
- Day 0 规范 JSON、SHA-256 与初始状态验证；schema/数据库完整性/外键检查；损坏数据明确拒绝，不自动重建。
- BEGIN 中断清理与事务所有权；严格 model_id 和无 NUL 的 UTF-8 文本契约；普通工作区/裸 Git 仓库及目录链接的防误放检查。
- 仓库外数据根路径；仅有合成开发数据演示，未正式启用真实伙伴身份。
- Windows GitHub Actions 工作流配置。远端 CI 结果须以实际运行记录为准。

**尚未实现**：记忆记录存储/检索、成长候选生成/接受、Day 0 独立导出包、完整备份恢复、schema 升级器、隐私删除工具、模型迁移评估。

**后续计划**：先完成私有开源准备和独立审查；真实本地模型与基准需要单独授权，流式输出、取消、严格总时限另行实现。M3 验证记忆和成长；M4 验证完整备份恢复与迁移；之后考虑主动互动、AIRI、语音、Live2D 和游戏。正式个人伙伴数据尚未启用。

**尚未验证**：真实模型质量、GPU 性能、跨模型行为连续性、长期偏好形成效果、AIRI 接入可行性。

默认演示仍为 FakeModel；HTTP 演示默认启动自己的随机端口合成服务，不连接已有 Ollama。真实服务访问要求同时显式启用 `--real-service --endpoint --model`；本轮没有执行该模式。M1 内存路径退出后丢失会话；SQLite 合成路径能跨进程恢复已提交数据。已验证持久化不代表完成备份恢复。

## Windows PowerShell：直接离线测试

前提：已安装对应 CPython 版本，Git 在 PATH 中（CI 回归测试需要它）。以下分别验收 Python Launcher 中的 3.12/3.13，在仓库根目录执行：

```powershell
py -3.12 -B -I -S tools/check_loopback_tests.py
py -3.13 -B -I -S tools/check_loopback_tests.py
```

这些命令无需安装项目、公网、Ollama、CUDA 或 GPU。HTTP 测试进行真实回环通信；审计守卫拒绝主进程非回环连接和主机名解析，HTTP 重启子进程有独立守卫。其余子进程只运行本地 Git、SQLite 和 FakeModel，审计不宣称覆盖 OS/全部子进程。`tools/run_tests.py` 原入口仍可用。测试和临时 Git 历史全部为合成内容，不修改用户配置。

## 安装与演示

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m genesis
.\.venv\Scripts\genesis-demo.exe
```

演示始终使用固定合成输入，输出 `Fake response: synthetic hello`；不接受真实聊天输入。

首次安装的构建隔离环境可能从包索引获取 `setuptools`，它是构建依赖，不是模型或运行时依赖。若当前 Python 环境已有固定的 `setuptools==84.1.0` 构建后端，可离线安装：

```powershell
python -m pip install --no-index --no-build-isolation --no-deps -e .
```

没有构建后端时使用直接测试命令，不要将联网安装称为离线测试的前置条件。无需激活虚拟环境或修改 PowerShell 执行策略。

## 合成持久化演示：两个独立进程

SQLite 使用 Python 标准库，需要 SQLite 3.37+（STRICT 表）。以下只创建临时合成数据，不使用默认正式数据目录；相同目录的第二次运行返回相同 UUID、Day 0 摘要和回复，模型调用数从 1 变为 0：

```powershell
$demoRoot = Join-Path ([IO.Path]::GetTempPath()) ("genesis-synthetic-" + [guid]::NewGuid().ToString("N"))
py -3.12 -B -I -S tools/run_persistence_demo.py --data-root $demoRoot
py -3.12 -B -I -S tools/run_persistence_demo.py --data-root $demoRoot
```

安装后亦可使用 `genesis-persistence-demo --data-root $demoRoot`。演示只接受数据根参数，输入为固定合成文本。不会自动删除该目录；它不是备份工具。持久化契约及故障状态见 [M2-A 持久化说明](docs/PERSISTENCE.md)。

## 合成 Ollama HTTP 演示（不是 LLM）

```powershell
py -3.12 -B -I -S tools/run_ollama_demo.py
# 安装后：
.\.venv\Scripts\genesis-ollama-demo.exe
.\.venv\Scripts\python.exe -I tools/check_persistence_entry.py
```

默认只使用自建合成 HTTP 服务、固定中文输入和临时 SQLite，结束自动清理；断言只发送一次 POST，重新打开数据库后重放不再调用 HTTP。进程重启场景由正式测试另行验证。没有 Ollama 安装、模型加载或下载。

只支持显式 `http://127.0.0.1:端口` 或 `http://[::1]:端口` 等回环 IP origin，不接受 localhost/DNS、凭据、路径、代理或重定向。连接/读写 socket 超时不是严格端到端总时限；模型名精确匹配且 `done=true, done_reason=stop` 才可成功。回环连接不能证明服务端没有使用云端模型，真实验收必须单独核实服务端云功能与运行位置。详见 [Ollama 契约](docs/OLLAMA.md)。

## 代码入口

```text
src/genesis/
  identity.py          伙伴身份
  core.py              内存/可选持久化编排及 ConversationStore 协议
  data_paths.py        仓库外数据根解析
  day0.py              规范初始状态与摘要验证
  storage/             SQLite schema、事务、事件与尝试账本
  models/contracts.py  模型输入/输出与失败契约
  models/fake.py       合成测试模型
  models/ollama.py     回环 HTTP 模型适配器，无状态写权限
  memory.py            观察、推断、证据与存储协议
  growth.py            成长候选及提议协议，无应用算法
  policy.py            默认拒绝的动作策略
  adapters/console.py  合成演示入口
  adapters/persistence_demo.py  合成持久化演示
  adapters/ollama_demo.py       默认合成 HTTP 演示
  adapters/synthetic_ollama.py  测试协议服务，不是 LLM
tests/                 合成场景、契约与边界测试
tools/run_tests.py     无安装测试入口
```

重复请求以 `(session_id, input_id)` 为键：成功请求重放；内容冲突报错；不同 ID 的相同文本是独立用户输入。原 M1 内存路径保持原样。SQLite 路径在短事务接收输入时即绑定 ID 与内容，失败后也不能改写该 ID 的内容；相同内容可显式重试。成功轮次与回执为同一行，提交后返回中断可跨进程重放，不承诺模型执行恰好一次。持久化重放保证值相同，不保证 Python 对象 `is` 相同。

两条路径的文本规则现在统一为非空白字符串、无 NUL、可严格编码为 UTF-8；model_id 也使用这一规则。中文、换行、组合字符、有效 Unicode 和原始空格保持原貌，不做隐式 str() 或文本修补。无效输入抛 TextContractError（ValueError 子类）；模型契约失败由 Core 包装为 TurnGenerationError，数据库不产生成功轮次。

证据核验使用 `candidate.independent_evidence_ids(host_registry)`；登记表必须来自宿主可信输入渠道，不能从模型提出的来源列表构造。登记缺失、引用不匹配或根来源未知时不计入独立证据。它只核验来源，不判断事实、语义相关性或偏好是否成立。

## Windows CI 的提交范围

工作流完整检出历史，运行 `tools/check_ci_diff.py`，权限保持 `contents: read`：

- 普通分支 Push：检查事件 `before` 到 `after`，覆盖一次推送的全部净变更。
- 新功能分支首次 Push：使用事件指定的默认分支远端引用与 `after` 的共同祖先作基线，避免只检查最后一个提交。
- 默认分支首次 Push：仅接受全零 `before`、`created=true`、事件默认分支与 ref 一致、`after`/checkout SHA/实际 HEAD 相同且原始 commit 无父提交；通过 `git diff-tree --root --check -r` 检查根提交的全部新增文件，不比较 HEAD 自身。浅克隆边界不是根提交。
- Pull Request：检查事件 base/head 的共同祖先到 PR head；测试仍使用 GitHub 默认检出的合并结果。
- 手动运行：必须填写完整 `base_sha`，与该次检出 SHA 比较。

除上述经严格验证的根提交分支外，缺失提交、远端基线或共同祖先会明确失败，不自动联网补取或回退到 `HEAD^`；标签推送不触发此工作流。M2-B1、P1 与 P2-A 的历史远端验收归属保留。截至 2026-11-03，新独立仓库仍为 Private；参考提交 [`0696e2279df7ef87a95a2ba5a6934440cbe166c4`](https://github.com/NanaseAki/genesis-core/commit/0696e2279df7ef87a95a2ba5a6934440cbe166c4) 的普通推送及 [双版本 CI](https://github.com/NanaseAki/genesis-core/actions/runs/41638648429) 已核验通过。首次根推送的原始事件字段仍未取得，所有者已接受推送回执、远端提交及根差异核验作为限定替代证据；这不等于复原首次事件，首次 CI 失败记录继续保留。上述结论只属于所列提交，不自动覆盖后续改动，也不代表获准公开或发行。

矩阵保留 Python 3.12/3.13、`contents: read`、真实发行 Canary、受控构建、安全重建及验证后 wheel 安装，并独立运行持久化与合成 HTTP console。所列 CI 在每个版本分别完成源码、无 `.git` 快照和安装后三轮测试，每轮均为 289 个唯一测试，包含 6 项 replacement 测试；无 `.git` 快照测试前后再次 verify-only。工作流为首次默认分支根 Push 设计了限定事件字段输出及根差异检查，但不能据此补称历史缺失字段已取得。CI 日志是平台文本记录，不冒充本机显式审计的原始二进制流或 OS 进程证据。

PR 合并检出与 head SHA 的区别见 [GitHub 事件文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)。

## 数据与开发约束

所有真实记忆、数据库、备份、密钥、录音和模型权重默认保存在仓库外。`.gitignore` 只是防误提交措施，不能代替数据隔离或秘密管理。不要使用 `git add -f` 绕过它。

显式创建 SQLiteStore 时，默认数据根为 Windows `%LOCALAPPDATA%\Genesis`，支持 `GENESIS_DATA_ROOT` 与显式绝对路径覆盖；检查原路径和解析后的路径，拒绝普通 Git 工作区及常规裸仓库内部。裸仓库识别结合 HEAD、objects、refs/packed-refs 与 core.bare 配置，不强制安装 Git。导入包和原 M1 演示均不创建数据库。M2-A 数据文件固定为 `genesis-synthetic.sqlite3`，Day 0 标记为合成开发数据。默认回复策略不是 Python 沙箱，存储/模型适配器属于可信宿主代码。

现有实现保持物理 v1 schema 和 Day 0 格式；已有 v1 数据若包含文本契约禁止的内容，将明确拒绝打开，文件保持原样，不自动清洗或迁移。具体规则见 [持久化契约](docs/PERSISTENCE.md)。

## 私有发行准备

开发安装命令用于本地验证，不能直接当作公共发行流程。`.gitignore` 不控制安装包的全部输入。受控准备只读取指定完整提交中的 [显式文件清单](release_files.json)，在仓库外临时暂存，并逐项检查源码、wheel 和 sdist。详见 [源码快照与发行物检查](docs/RELEASE_PREPARATION.md)。

生成物始终标记为私有准备材料，不是许可证、公开或发布授权；不携带原 `.git`、旧提交身份或私有审计报告。

## 项目文档

- [愿景与边界](docs/VISION.md)
- [架构与接口契约](docs/ARCHITECTURE.md)
- [M2-A SQLite schema、事务与恢复边界](docs/PERSISTENCE.md)
- [M2-B1 Ollama HTTP、错误与本地推理验收边界](docs/OLLAMA.md)
- [源码快照与发行物检查](docs/RELEASE_PREPARATION.md)
- [Alpha 说明草案](docs/ALPHA_RELEASE_NOTES.md)
- [第三方来源清单草案](docs/THIRD_PARTY_SOURCES.md)
- [Apache-2.0 决定与实施前核验](docs/LICENSE_OPTIONS.md)
- [贡献约定](CONTRIBUTING.md)、[安全报告边界](SECURITY.md)、[行为准则](CODE_OF_CONDUCT.md)
- [里程碑与验收](docs/ROADMAP.md)
- [数据、Day 0、备份、迁移及删除规范](docs/DATA_PORTABILITY.md)
- [开发者与编码代理约定](AGENTS.md)
- [ADR 0001：核心边界与 M1 范围](docs/decisions/0001-core-foundation.md)

## 许可与归属

Copyright 2026 NanaseAki

所有者有权许可的 Genesis Core 源码、测试、开发工具、配置和原创文档适用 [Apache License 2.0](LICENSE)。该版权声明仅适用于有权许可的项目原创部分，不声明全部第三方内容由 NanaseAki 独占所有。`.gitignore` 前 220 行来自 GitHub Python.gitignore 模板，保留 CC0 来源；详见 [第三方来源与例外](docs/THIRD_PARTY_SOURCES.md)。有限来源核验未发现具体未解决许可冲突，不是全部原创或完全清权的保证。当前无已识别需单设 NOTICE 的适用通知，本分支不创建 NOTICE；后续发现具体材料时必须按原条款处理。正式许可不改变本项目仍处于私有准备、尚无公开发布操作授权的状态。
