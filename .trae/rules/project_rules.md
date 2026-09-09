# 墨流（InkFlow）项目规则

开始工作前读取根目录 `AGENTS.md`，再检查当前小说项目的 `BOOK.md`、`PLAN.md` 与 `STATE.md`。产品名称固定为墨流（InkFlow）。

用户通过自然语言控制工作流，TRAE 负责调用 InkFlow MCP 并把需求交给 Coordinator。写章必须经过 Coordinator 任务单、四级规划、Context Packet、Writer、Reviewer、用户验收、Memory Keeper 和 SQLite 事务提交。不得直接修改 `.inkflow` 数据库，也不得把 API Key 写进项目文件。
