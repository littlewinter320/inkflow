# 墨流 InkFlow for VS Code

这是墨流 0.2 的 VS Code 适配器。它提供小说树、行级批注、Writer 定点修订、Reviewer 审查、项目状态、任务记录、安全检查点和 MCP 配置。

扩展不会保存 API Key，也不会直接修改 `.inkflow/inkflow.db`。已验收正文的修改仍受墨流正史门禁保护。

## 安装

1. 在 VS Code 打开“扩展”页面右上角菜单。
2. 选择“从 VSIX 安装”，打开 `inkflow-vscode-0.2.0.vsix`。
3. 用 VS Code 打开一本包含 `.inkflow/project.json` 的墨流小说项目。
4. 从左侧“墨流”图标进入小说工作台；标题栏可以刷新、创建检查点、查看任务记录或合并 MCP 配置。

发布版 VSIX 自带 `inkflow-engine.exe`，普通用户不需要 Python。源码开发时，扩展也可以寻找工作区 `.venv`，或读取设置 `inkflow.enginePath` / `inkflow.pythonPath`。

## 主要操作

- 在 Markdown 中选中文字，右键“墨流：批注选中文字”或“让 Writer 修订选中文字”。
- 打开章节文件后右键“墨流：审查当前章节”。
- 使用“墨流：为当前工作区配置 MCP”把墨流安全合并进 `.vscode/mcp.json`；已有其他 MCP 配置不会被覆盖。
- “创建安全检查点”只保存当前状态；正式回退仍需在桌面版或 MCP 中先预览、再确认。
- 任务记录只显示状态和可复核摘要，不显示模型原始思维链。
