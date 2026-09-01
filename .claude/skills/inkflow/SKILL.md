---
name: inkflow
description: Use the InkFlow MCP server to plan, write, review, and commit long-form Chinese web fiction.
---

# 墨流工作流

先读取项目的 `BOOK.md`、`PLAN.md`、`STATE.md`，但不要遍历 `.inkflow`。通过 MCP 完成正式状态变更：

1. 没有项目时调用 `novel_project_create`。
2. 没有四级规划时调用 `novel_plan_generate`。
3. 写章前调用 `novel_context_build`，再调用 `novel_chapter_write`。
4. 草稿完成后调用 `novel_chapter_review`。
5. 若结论需要修改，调用 `novel_chapter_revise`；它会让 Writer 读取当前草稿与同版本审查报告。修订后必须重新调用 `novel_chapter_review`。
6. 只有用户接受当前已审版本后才调用 `novel_chapter_accept`。若 Memory Keeper 仍有冲突，向用户展示工具返回的具体冲突和 `reviews/*memory-conflict.md`，不得擅自 `force`。
7. 用 `novel_trace_read` 展示规划依据、工具、证据和用量。

API Key 不得出现在消息、文件或工具参数中；让用户使用安全凭据配置流程。
