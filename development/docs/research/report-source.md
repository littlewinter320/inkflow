# 墨流 v0.1 桌面版补充研究与范围基线

> 面向：墨流产品开发与后续新对话恢复
> 核对日期：2026-09-02
> 结论性质：外部项目只提供设计证据；是否已实现必须以本仓库代码和测试为准。

## 直接结论

墨流 v0.1 不应只给现有 Python 引擎套一个聊天窗口。一个真正可用的小说桌面版至少要同时提供：

1. 书籍、卷、篇章、章节、场景节拍的分层规划；
2. 人物在当前场景的目标、知识、误解、情绪与位置状态；
3. 可手动写作的正文编辑器，以及 AI 建议的逐项接受/拒绝；
4. 草稿、审查、正史和回退之间明确、可见的边界；
5. 能解释召回来源的 Context Packet，而不是把全部摘要塞进模型；
6. 桌面 EXE、VS Code 扩展和 MCP 使用同一个 Python Novel Engine。

本版保持 Writer、Reviewer、Memory Keeper 三个正式小说 Agent，不引入 Director、Character Agent 或第四个小说 Agent。新能力作为规划数据、上下文、确定性检查和界面呈现存在。

## 研究支持的关键取舍

### 动态分层规划，而不是一次写死全书细纲

- DOME 把粗纲作为全局承诺，细纲随着已经生成的故事逐步展开，使后续规划能够适应真实写作结果；同时用时间知识结构补充上下文一致性。
- DOC 的实验表明，更详细的层级大纲和细粒度控制能提高长故事的情节一致性、与大纲的相关性和可控性。
- Re3 说明“总体计划 + 当前故事状态 + 候选修订”优于一次性直接生成长故事。

墨流采用：全书罗盘保持稳定，卷/篇章维持滚动窗口，章节卡下增加场景节拍；篇章结束后先复审实际正文，再由用户决定是否调整未来细纲。

来源：

- [DOME: Dynamic Hierarchical Outlining with Memory-Enhancement](https://aclanthology.org/2025.naacl-long.63/)
- [DOC: Improving Long Story Coherence With Detailed Outline Control](https://aclanthology.org/2023.acl-long.190/)
- [Re3: Generating Longer Stories With Recursive Reprompting and Revision](https://aclanthology.org/2022.emnlp-main.296/)

### 写作软件需要“场景”和“故事圣经”，不能只有章节文件

- Novalist 把场景状态、人物/地点/物品、情节线矩阵、时间线、人物出场记录和对白声线集中呈现。
- Manuscript 提供场景拖拽、多个情节线、故事圣经、快照 Diff、局部接受、全文查找和写作统计。
- novelWriter 证明纯文本、项目树、统计、可选择的稿件构建和定期备份对长期写作仍然重要。
- xnovelist 的位置相关连续性表明，第 N 章应该继承第 N-1 章确立的状态，高信号故事圣经不应被静默截断。

墨流 v0.1 因此新增：场景节拍、故事/文风圣经视图、人物意图与知识边界、时间地点状态、建议式局部修改、全文搜索、字数与对白比例。复杂的自由软木板、世界历法和出版排版延后。

来源：

- [Novalist 功能与数据组织](https://github.com/Drommedhar/novalist-official)
- [Manuscript 写作与修订功能](https://github.com/DoktorDaveJoos/manuscript)
- [novelWriter](https://github.com/vkbo/novelWriter)
- [xnovelist Story Bible](https://github.com/giapnguyen74/xnovelist/blob/main/docs/STORY_BIBLE.md)

### 桌面、VS Code 和 MCP 应共享一个协议与引擎

- Codex app-server 使用 Thread / Turn / Item 与流式事件，把后端 Agent 和富客户端解耦。
- DeepSeek Harness 的前端由事件日志投影状态，并通过精确文件替换和 Diff 呈现变更。
- VS Code 官方支持扩展注册 stdio MCP Server，也支持工作区 `.vscode/mcp.json`；Tree View 应优先使用原生 API，复杂详情才使用 Webview。

墨流采用本地 JSONL RPC：Electron、VS Code 扩展和测试客户端都调用同一个 `inkflow app-server`。MCP Server 保持独立兼容入口，并增加资源、提示词、工具注解与安装辅助。

来源：

- [OpenAI Codex app-server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [DeepSeek Harness 架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.zh.md)
- [VS Code MCP 开发指南](https://code.visualstudio.com/api/extension-guides/ai/mcp)
- [VS Code Tree View 指南](https://code.visualstudio.com/api/extension-guides/tree-view)
- [VS Code Webview 指南](https://code.visualstudio.com/api/extension-guides/webview)

## v0.1 必做范围

### 引擎和协议

- 版本化 JSONL RPC 与流式任务事件；
- 项目列表、创建、打开、状态、正文读取与安全保存；
- 自然语言会话、规划、单章/批量生成、审查、修订、接受和回退；
- 章节版本、行级批注、建议修改和检索接口；
- Provider 配置与 Windows Credential Manager；
- 旧 MCP 工具保持兼容，并补资源、提示词和行为注解。

### 桌面端

- 左侧小说树、中间对话、右侧工作台；
- 正文编辑、自动保存、字数统计、Diff、批注；
- 规划、审查、记忆、Context Packet、任务过程和检查点面板；
- 项目向导、模型设置、错误恢复；
- Windows 安装 EXE，用户无需另装 Python 或 Node。

### VS Code 扩展

- 墨流项目树和当前状态；
- 打开正文/规划/审查/记忆；
- 对编辑器选区发起批注或 Writer 定点修改；
- 注册或生成墨流 MCP Server 配置；
- 启动桌面墨流和打开当前项目；
- 打包为 VSIX。

## 本版明确不冒充完成的能力

- 完整向量数据库和本地 Embedding 模型；
- 复杂世界历法、关系图、自由软木板和出版级排版；
- 公网远程、云同步、SSH、Git 和在线插件商店；
- 自动绕过登录、付费、DRM 或平台访问控制的爬虫；
- 原始模型思维链展示；
- 未经测试的多供应商原生协议。v0.1 只承诺 DeepSeek 预设与通用 OpenAI 兼容接口。

## 许可证与复用边界

墨流继续使用 MIT。GPL/AGPL、无许可证、限制商用或附加商业条款的仓库只研究交互和数据思想，不复制源码或提示词。实际依赖必须生成第三方许可证清单。

## 研究局限

外部桌面项目主要通过当前仓库文档与源码结构核对，没有逐个安装运行。论文结果的篇幅、语言、模型和墨流中文网文场景并不完全相同，因此只用于确定架构方向；最终效果仍需以墨流自己的长篇样本和用户验收为准。
