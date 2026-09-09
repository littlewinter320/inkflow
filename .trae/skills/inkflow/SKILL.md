# 墨流四 Agent 小说创作 Skill

适用于创建小说、规划卷与篇章、写章节、审查、修订、提交记忆和分析参考作品。

优先使用 `novel_*` MCP 工具。由 Coordinator 理解自然语言、拆解和派工，宿主不冒充四个正式 AI Agent。先查状态再行动；写作前确保章节卡存在；审查未通过时调用 `novel_chapter_revise` 并重新审查，不得自动接受；Memory Keeper 返回冲突时展示具体冲突及 `reviews/*memory-conflict.md`，不得擅自 `force`；每次把任务单、Trace 和受影响文件告诉用户。
