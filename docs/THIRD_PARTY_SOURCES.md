# 第三方来源与许可例外

本清单用于后续权利人复核，不是所有代码均原创或已获授权的法律保证。不得擅自删除现有第三方通知；新材料需记录来源、版本、修改及再分发条件。

| 项目/材料 | 使用方式 | 来源与许可边界 |
| --- | --- | --- |
| Genesis Python 代码与合成测试 | 项目实现，没有 vendored 模型/HTTP 库 | 所有者有权许可的项目原创部分在本分支采用 Apache-2.0；AI 辅助或单一提交身份不是完整权利证明 |
| Python 标准库 | 运行时依赖，未打包解释器 | [PSF 许可](https://docs.python.org/3/license.html)；若未来分发解释器须盘点其全部通知 |
| SQLite | 使用标准库 sqlite3，未捆绑数据库二进制 | [SQLite 核心声明](https://www.sqlite.org/copyright.html)为 public domain；上游构建辅助文件可能有其他条款 |
| Ollama | 参考公开 /api/chat 协议，无服务源码或权重 | [协议文档](https://docs.ollama.com/api/chat)、[服务 MIT LICENSE](https://github.com/ollama/ollama/blob/main/LICENSE)；模型各自条款独立，不能覆盖 Genesis |
| setuptools | 构建时使用，不是运行时依赖 | [MIT LICENSE](https://github.com/pypa/setuptools/blob/main/LICENSE)；本机实际版本在验证报告中记录 |
| GitHub Python.gitignore | .gitignore 前 220 行为模板，后接项目规则；模板保持 CC0 | [模板](https://github.com/github/gitignore/blob/main/Python.gitignore)与 [CC0-1.0](https://github.com/github/gitignore/blob/main/LICENSE)；保留来源说明 |
| actions/checkout、setup-python | CI 引用固定提交 SHA，不 vendoring 实现 | 各上游 MIT；见工作流的固定 SHA 和对应上游 LICENSE |
| 文档、字体、图像、声音、模型素材 | 当前无分发的外部二进制素材 | 引用链接不是复制授权，也不是品牌背书；未来引入时逐项审查 |

Neuro-sama 为愿景启发，AIRI/Live2D 为后续候选方向；不分发其角色形象、源码、模型或品牌素材。项目不声明官方关联。

Copyright 2026 NanaseAki 仅指所有者有权许可的项目原创部分，不覆盖上述第三方材料。有限来源核验没有发现除已声明 CC0 模板外需要立即补许可的具体复制实现；未完整还原 AI 生成来源链，不保证全部原创或完全清权。调用公开 API、标准库和外部构建工具不等于随包分发其实现；若后来确认实际复制或改编，必须按具体文件及原许可补通知、取得许可、替换或排除。

模板内容在本次核验与 [github/gitignore 固定对照版本](https://github.com/github/gitignore/blob/0e5d690153ca3da8a4a1aef2d053406f408f531c/Python.gitignore) 一致；这是核验对照版本，不宣称找回最初复制时的 commit。保留 [CC0 条款](https://github.com/github/gitignore/blob/0e5d690153ca3da8a4a1aef2d053406f408f531c/LICENSE)及本说明，不把模板重新宣称为项目原创。当前没有识别到必须单设 NOTICE 的适用通知，因此不创建 NOTICE；未来第三方条款优先按实际材料保留。

外部 setuptools 主许可为 MIT，但含 vendored 材料；本项目不分发该构建工具。未来捆绑解释器、构建器、模型或素材时必须重做实际分发清单。
