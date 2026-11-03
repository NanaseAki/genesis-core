# 受控源码快照与发行物检查

状态：P1 与 P2-A 已审查、远端验收并合并，R4 在受控边界内技术关闭；P2-A 历史基线为两版各 270 项源码/安装测试。Apache-2.0 及许可证专项校验已实施，原字节干净基线已按批准的修订合同完成 Python 3.12/3.13 本地限定验收，各 289 项完整测试（其中 6 项 replacement）。本候选仅完成文档与有限 CI 静态适配，修改后文件集尚未重新测试或构建；新目标首次 Push/CI 和公开发行仍待单独验收。工具不建公共仓库或上传内容。

该本地验收采用显式审计入口，原始输出按调用边界记录，未补齐旧 pip 内部逐写入者归属；默认 HTTP console 的原临时现场未保留，独立留存场景不能替代它。四项启动 flags 与固定子进程环境仅按锁定启动链作条件推导，未观察值不作已观察事实；不声明操作系统级全进程、文件或网络隔离。既有失败与未知记录保留，成功结果不回填历史缺失证据。

所有者已批准 Copyright 2026 NanaseAki，仅适用于有权许可的原创部分；GitHub Python.gitignore 前 220 行保持 CC0 来源。正式许可证为 [LICENSE](../LICENSE)，范围及来源限制见 [许可说明](LICENSE_OPTIONS.md)。当前没有必须创建 NOTICE 的已识别适用通知，故没有 NOTICE；工具不接受未批准 NOTICE。贡献按已确认 Apache-2.0 政策，初期无 CLA/DCO。

旧仓库按既定方案独立保留 Private；未来独立快照/公共唯一主线方案已确认，目标仓库及 PVR 状态须在相应阶段核实，本候选不声明公开入口或 PVR 已启用；真实 Ollama/GPU 未验收。LICENSE 不等于 Public、Release 或 PyPI 操作授权，不宣称全部原创或完全清权。

## 批准边界

release_files.json 是逐文件、无通配符的公开候选清单。清单和 MANIFEST.in 的每条 include 必须一致。变更清单及其指定提交中的内容均须人工审查；路径允许不代表内容自动无秘密，也不代表版权已确认。工具只接受完整 40 位 commit SHA，不接受 HEAD/分支缩写等可变引用。

输入来自该提交的 Git tree/blob，不来自开发工作树。所有源码 Git 命令同时使用 `--no-replace-objects` 和清理后强制设置的 `GIT_NO_REPLACE_OBJECTS=1`，忽略但不删除已有 replace refs；读取的每个 blob 再计算 Git 对象 SHA-1 并核对 tree 中的 OID。commit/tree/清单/构建配置和普通模块均受此约束。未跟踪、忽略和已提交但未批准的额外文件都不读取、不导出。只允许受限 ASCII 相对路径、批准目录和文本类型；拒绝绝对路径、反斜杠、遍历、大小写冲突、Windows 保留名、数据/私有/审计路径、超预算文本、Git symlink/gitlink。合法 Unicode 文件内容保持原字节。

源码默认包含完整 Genesis 模块、合成测试及 helper、工具、公开价值文档、必要 Git 文件和选定 Actions/模板。私有交付/复审报告不在清单内；从前的 .git、提交作者/邮箱/签名及私有审计证据不导出。

不要把 .gitignore 当作打包边界，也不要直接从含未知工作树文件的目录构建公共包。pyproject 显式列出包并关闭自动数据收集只是补充防线，不能阻止批准包内的额外 .py 被直接构建。

## 流程与上限

1. 用审核过的工具读取指定提交中的清单、文件模式和 blob，核验每项大小、类型与路径。
2. 在独立 TemporaryDirectory 创建纯源码暂存，逐项核对文件集合与原字节；不允许符号链接、junction、特殊文件或额外空目录。
3. 将暂存复制到另一临时构建目录，仅调用已安装的 setuptools.build_meta（当前固定为 84.1.0，pyproject 与工具均校验版本）；不运行 pip，不安装/下载依赖。需调用者提供可信的、已具备构建后端的 Python。
4. 实际打开 wheel/sdist，不解压到任意路径；检查每个成员的路径、类型、重复/大小写冲突、声明大小和实际读取，以及精确文件集合。批准源码字节必须相同；wheel RECORD 的集合、SHA-256 和长度必须正确。
5. 不仅检查元数据文件名：从批准 pyproject、README 和文件清单独立推导预期内容，按下述闭合规则核验。sdist 必须含全部批准测试、helper、tools 与公共文档，不能只依赖 setuptools 默认的 tests/test*.py。
6. 全部通过后才复制到仓库外的新目录，再核验一次。已有输出目录、Git 仓库内输出及链接祖先拒绝，不覆盖旧工作。

单文件上限 1 MiB，源码最多 512 项/16 MiB；安装包检查对条目数量、单项和解码总量有界。允许 tar 的数值 mtime PAX 元数据，拒绝路径/链接/尺寸覆盖、硬链接、特殊类型等。构建后端和本机文件系统属于可信边界，不声称抵抗恶意并发替换路径、恶意构建解释器或任意本机攻击。

输出包括 source/、dist/ 与 SNAPSHOT_CHECKSUMS.json。摘要列出每个批准源码的 SHA-256/长度、两个实际包的文件清单、逐文件内容摘要及包摘要，状态固定为 private_preparation_not_authorized_release。没有原始作者元数据。源码集合/字节可按同一提交重复验证；构建时间及后端差异可能改变包字节，不宣称 wheel/tar 整体逐字可复现。摘要不是签名或版权证明。

构建失败不公开任何包；复制输出期间的磁盘故障可能留下不完整的新目录，须人工检查，不能将其当作验收成功。工具不清理用户原有目录。

## Windows 命令

从已审查的 Git 工作区运行，先准备已有 setuptools 的可信 Python；示例不隐式安装后端：

```powershell
$sourceCommit = git rev-parse HEAD
$buildPython = (Get-Command python).Source
$output = Join-Path ([IO.Path]::GetTempPath()) ("genesis-private-preparation-" + [guid]::NewGuid().ToString("N"))
py -3.12 -B -I -S tools/release_prepare.py --repository (Resolve-Path .).Path --commit $sourceCommit --output $output --build-python $buildPython --private-preparation
py -3.12 -B -I -S tools/release_prepare.py --repository (Resolve-Path .).Path --commit $sourceCommit --output $output --verify-only --private-preparation
```

不要直接把未提交工作视为已批准提交；清单、工具、配置和源码都应在获得相应授权后成为可审查的本地提交。文档或 CI 适配后的新候选不能沿用原字节基线收据，必须绑定自己的批准文件摘要及准确新根 commit，再按单独授权进行受控构建与验证。许可证已实施，但输出仍用于私有准备验证，不能自动上传或发布。

verify-only 无需 setuptools，不执行候选配置，不只信任已保存的摘要：它重新读取固定 Git blob，核对纯源码、实际安装包和收据。收据不能冒充新的来源或法律授权；重新构建的包若时间不同须使用各自真实收据。

离线真实构建 Canary：

```powershell
py -3.12 -B -I -S tools/check_release.py --commit $sourceCommit --build-python $buildPython
py -3.13 -B -I -S tools/check_release.py --commit $sourceCommit --build-python $buildPython
```

该检查在临时副本复现旧发现规则将两个私有模块装包，再验证新流程排除全部合成 Canary，并注入额外源码/包成员、篡改 console/setup/SOURCES/comment、许可正文/许可元数据及非零 tar 目录并重算收据，验证实际校验会失败；同时验证合法 sdist 到 wheel 的重建。临时 Git 历史全部为合成身份，不复制私有历史，结束清理。普通单元测试无需构建后端；真实构建检查是另一步，不能用单元测试通过冒充已经执行。

Windows CI 在 P1 已实际完成双版本源码测试、真实 Canary、受控构建、verify-only、安全重建、验证后 wheel 的离线安装和既有入口验收。CI 先从 PyPI 安装精确版本 setuptools==84.1.0 并核对实际版本；该联网 bootstrap 与后续离线检查分开，不宣称版本固定等同供应链已完整验证。新独立目标的首次 Push/CI 尚待验收，不能把历史成功或原字节基线本地验收沿用为本候选的远端结果；分发物 hash 锁定另列改进，本候选仅增补无 .git 全量步骤与首次根 Push 的白名单事件日志，改动尚未动态验证。

解包源码的测试用独立临时合成 Git fixture 验证数据目录拒绝，不要求候选自身含 .git，也不得向候选写运行数据。验收时除 checkout 与验证后 wheel 安装测试，还运行无 .git 快照的完整双版本测试，再次 verify-only 确认测试没有改变批准文件集合。

本候选的 CI 在 verify-only/roundtrip 成功后、wheel 安装前，从同一次 releaseOutput/source 运行原完整入口；执行前和返回后均以固定 commit 再次 verify-only，拒绝额外文件、摘要漂移、链接或 Git 仓库祖先。按当前测试布局从源码文本推导完整方法 ID，核对 289 个唯一成功结果及其中 6 个 replacement，不以单一退出码或汇总行替代。测试输出是解码后合并的 CI 文本，未声明独立原始双流；超时与 runner 中断仍受平台约束，未完成的后验检查不能算通过。这一新增流程尚未动态验证。

新仓库首次默认分支 Push 必须满足严格根提交条件：全零 before、created=true、默认 ref、after/checkout SHA/实际 HEAD 一致，且原始 commit header 无 parent。差异工具使用独立的 `diff-tree --root --check -r` 检查全部新增文件，浅克隆伪根或其他缺失基线不回退；普通 Push/PR 的基线规则保留。本候选在原检查成功后补充限定字段日志，不打印完整事件、环境或提交作者；日志只在真实首次默认分支根 Push 路径产生。本候选仍无该真实事件或 CI 运行结果，不沿用历史合成事件结果作为证明。

## 元数据的独立可信依据

当前仅支持审核过的 setuptools 84.1.0 纯 Python wheel/sdist 格式。升级后端、项目元数据字段或构建格式必须同时审查规则与回归；不从待检查包或收据生成“预期内容”。校验不需要参考候选包执行任何配置。

- METADATA 与两份 PKG-INFO 只允许 Metadata-Version 2.4、批准名称/版本、可选 Summary/Requires-Python、Markdown 内容类型、License-Expression: Apache-2.0、License-File: LICENSE、Dynamic: license-file；正文匹配批准 README。最后三项来自固定 84.1.0 的独立参考构建，其中 Dynamic 仅允许这一精确值。License-File 按精确路径列表比较，拒绝重复、缺失、额外值、大小写变体及逃逸；其他重复/未知字段、作者邮箱、依赖和其他 Dynamic 均拒绝。
- pyproject 必须静态指定 license="Apache-2.0" 与 license-files=["LICENSE"]；不接受通配符、未知 NOTICE 或移除两项声明的降级。LICENSE 原始字节既绑定固定 Git blob，也固定核对 ASF 官方正文 SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`（11358 字节），不做换行归一化。wheel 仅增加 `.dist-info/licenses/LICENSE`，与源字节一致并纳入 RECORD；sdist 的根 LICENSE、SOURCES 与元数据逐项核验。更换许可/后端/文件集必须重新审核规则，不接受候选与收据自证。
- entry_points.txt 精确匹配批准 console scripts；top_level.txt 只能是 genesis；dependency_links.txt 必须为空。SOURCES.txt 每行是批准路径或明确生成的 egg-info 文件，集合精确、无重复、无额外载荷。
- setup.cfg 只接受后端生成的 egg_info 空 tag_build / tag_date=0 的完整格式。任何 data_files、package_data、aliases、命令、外部引用或额外注释均拒绝。
- WHEEL 只接受 1.0、固定后端 Generator、purelib=true 和 py3-none-any；RECORD 仍独立核验成员、摘要及大小。允许元数据文本的 LF/CRLF 差异，不放宽源码原字节检查。
- ZIP 不接受 archive/member comment、extra 字段、前缀/尾部/成员间隙载荷、加密、数据描述符或 ZIP64；校验 local/central framing 和压缩流完整性。当前受控后端的普通 wheel 正常支持；没有将整个 wheel 格式禁用。
- gzip 只接受普通头及与包名一致的可选 filename，拒绝额外流或尾部载荷；tar 只接受常规文件/目录及单一数值 mtime PAX 记录，拒绝额外 owner 字符串、路径覆盖、特殊条目、非零填充与隐藏尾部载荷。时间戳可变，禁止任意容器文本载荷。当前输出预算不需要 ZIP64、GNU长名或其他扩展。

tar 目录在原始记录和上层 tarfile 最终成员解释中均必须 size 为零，非零目录在跳过后续记录或读取成员正文前拒绝；PAX size 覆盖继续不支持并明确拒绝，不能借此改变两层的解析范围。

元数据解析规则是独立可信基准；SNAPSHOT_CHECKSUMS.json 是可重算的结果记录，不是信任根。仅篡改包或同时篡改包与收据，都必须重新通过来源、格式和容器检查。

## 已验证 sdist 的重建顺序

`--roundtrip` 必须在完整成员、源码及生成元数据验证成功后才将 sdist 文件写入新的临时目录并调用可信后端；失败候选不得先构建。重建 wheel 再次执行相同校验，并与原合法 wheel 的逐成员内容摘要比较。完整 archive 时间字节不要求相等。

```powershell
py -3.12 -B -I -S tools/release_prepare.py --repository (Resolve-Path .).Path --commit $sourceCommit --output $output --build-python $buildPython --verify-only --roundtrip --private-preparation
```

解释器、审核过的规则/源码与构建后端是可信边界，不声称通用恶意后端沙箱或抵抗本机恶意竞态。记录验证工具的 Python 与真正构建解释器版本；两者可以显式不同。方向决定已完成，公开仍需独立复审、最终许可/来源范围复核、目标提交身份实检、目标/PVR 安全验收和另行发布授权。
