# 贡献约定

Apache-2.0 已实施，见 [LICENSE](LICENSE)。贡献者必须有权提交所提供的内容；提交并被接受纳入本项目的贡献按 Apache-2.0 许可。第三方内容必须说明来源、版本和修改，保留适用版权、许可及通知；每个 PR 都经过审查。初期不采用 CLA 或 DCO，不要求额外版权转让。不具授权资格或存在未解决许可冲突的内容不能合入。

当前为独立 Core 私有准备，本候选尚未开放公开贡献入口；目标仓库状态和实际入口须在相应阶段核实。本政策不是建仓、推送或发布授权。第三方例外见 [来源清单](docs/THIRD_PARTY_SOURCES.md)。

支持 Windows 原生 CPython 3.12/3.13，Git 在 PATH 中。核心运行时只使用标准库；无需 Ollama、模型、CUDA 或私人配置。

在 Git 工作区根目录运行：

```powershell
py -3.12 -B -I -S tools/check_loopback_tests.py
py -3.13 -B -I -S tools/check_loopback_tests.py
git diff --check
```

测试只能使用合成内容和临时数据；不要从真实聊天抽样。禁止提交数据库、密钥、用户配置、伙伴档案、私人审计报告或模型权重。遇到安全或隐私问题先读 [SECURITY.md](SECURITY.md)，不要把原文放进 Issue。

每次修改说明问题、行为变化、实际验证和局限。模型只能提供回复，不能修改身份、权限、成长或数据库。变更持久化/来源/错误语义时必须更新契约和回归。不要绕过测试或降低断言。

新公开文件必须逐项加入 release_files.json 及对应 MANIFEST.in include 行，经审查后才可导出。文件清单变更是安全相关变更，不允许用通配符替代。发行验证见 [RELEASE_PREPARATION](docs/RELEASE_PREPARATION.md)。

提交作者身份是否公开由本人决定；使用获准的公开署名，勿把个人邮箱放入示例、构建元数据或问题模板。不改写他人历史或移除必要第三方通知。

合并、推送、Public、Release/PyPI 发布均需要各自明确授权；通过测试或合并准备 PR 不等于发布授权。
