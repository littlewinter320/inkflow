# 墨流（InkFlow）

请先完整阅读 `AGENTS.md`，再检查当前小说项目的 `BOOK.md`、`PLAN.md`、`STATE.md` 和 `DIALOGUE.md`（存在时）。不要遍历或直接修改 `.inkflow`。

与用户协作时以自然语言驱动 MCP 工具，不要求用户记终端命令。正式写章必须依次调用规划门禁、Context Packet、写作和审查工具；若审查要求修改，调用 `novel_chapter_revise` 让 Writer 读取当前草稿和同版本报告，之后重新审查；仅在用户验收后调用接受工具。Memory Keeper 仍有冲突时展示具体冲突与 `reviews/*memory-conflict.md`，不得擅自 `force`。不得直接把草稿当正史。过程结果应显示计划摘要、工具、证据、文件差异和 Trace 链接。
