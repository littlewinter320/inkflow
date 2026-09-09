# 墨流（InkFlow）Copilot Instructions

本仓库开发的是中文长篇网文四 Agent 协作引擎，其中 Writer、Reviewer、Memory Keeper 是三个小说生产 Agent。开始前读取 `AGENTS.md`，并以当前代码、测试与用户小说项目文件为准。

- 不把产品称为 InkFlow-3。
- 不把 API Key 写入仓库。
- 不绕过四级规划、审查或记忆事务。审查要求修改时调用 `novel_chapter_revise`，修订后必须重新审查当前版本；Memory 冲突必须展示报告并等待用户处理，不得自动 `force`。
- 不让四个 Agent 进行无版本、无证据、无轮次上限的自由群聊；它们通过结构化工件协作。
- 只按用户当前明确要求做定向验证；一次性验证放在临时目录或内联脚本，不向仓库提交测试、评测样本、旧计划或重复说明。
