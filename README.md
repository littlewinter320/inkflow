# 墨流（InkFlow）

面向中文长篇网文的本地三 Agent 创作引擎。你可以在 Claude Code、VS Code、TRAE、GitHub Copilot、Codex 等编程 Agent 中用自然语言完成规划、写章、审查、修订、验收、记忆同步和回退。

> 当前状态：`0.2.0` 私密预览版。Windows 10/11 桌面端、VS Code 扩展与通用 MCP 是主要交付形态；仓库尚未发布到 PyPI。

## 为什么是三个 Agent

- **Writer**：负责规划、写作和定点修订。
- **Reviewer**：只审查，给出有正文依据的扣分与问题，不直接改正文。
- **Memory Keeper**：只在用户验收后，把正文变化事务化写入 SQLite 正史。

会话主控只理解自然语言并路由，不是第四个小说 Agent；Python 编排器负责保证步骤顺序和文件一致性。

## 安装

### 普通用户：安装 Windows 版

拿到发布目录后，先双击 `InkFlow-Setup-0.2.0.exe` 完成安装，再打开“墨流 InkFlow”。第一次使用时：

> 当前私密预览安装包尚未配置商业代码签名证书，Windows 可能显示“未知发布者”或 SmartScreen 提示。可先用同目录 `SHA256SUMS-0.2.0.txt` 核对文件；正式公开分发前应接入可信代码签名证书。

1. 点击“模型设置”，填写兼容 DeepSeek/OpenAI 请求格式的地址、模型名和 API Key；Key 进入 Windows 凭据库、不写进小说文件。连接检查会真实询问模型，并同时显示模型回答和可复核的公开判断。这里还可把主动询问频率设为低、中、高、超高。
2. 点击“新建小说”。完全没有书名、题材或主角时，直接选“我没有想法，AI 来构思”：默认快速生成一个并自动选中，需要比较时再点“生成三个供比较”。确认后才建立本地项目；已有想法也可以切换为手动填写。
3. 在中间对话框直接说需求，例如“先和我讨论开篇方向，不写正文”或“批量写第 1～5 章草稿，逐章审查，先不要验收”。也可以点“先问我”，让墨流先提出 1～3 个真正影响方向的问题。
4. 在右侧“项目”页查看任务状态、失败恢复、检查点和回退预览；在“过程”页按任务卡查看阶段摘要和工具状态，不再混入初始化查询或重复底层事件。

安装包自带本地引擎，不要求普通用户安装 Python、Node 或使用终端。`inkflow-vscode-0.2.0.vsix` 是可选的 VS Code 适配器：在 VS Code 的“扩展”页面选择“从 VSIX 安装”，即可获得小说树、选区批注/修订、Reviewer 审查、检查点和 MCP 配置入口。

### 开发者：从源码运行

准备 Python 3.11 或 3.12，然后在 PowerShell 中执行：

```powershell
git clone https://github.com/littlewinter320/inkflow.git
cd inkflow
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\inkflow.exe configure-key
```

`configure-key` 会在不回显的输入框中读取 DeepSeek API Key，并写入系统凭据库。不要把真实 Key 写进 `.env`、聊天消息或小说文件。

验证安装：

```powershell
.\.venv\Scripts\inkflow.exe --help
.\.venv\Scripts\python.exe -m pytest
```

随后用支持 Agent 与 MCP 的编辑器打开仓库，要求编辑器 Agent “读取 AGENTS.md，连接墨流 MCP，并帮我创建一本小说”。Windows 下仓库自带 `.mcp.json`、`.vscode/mcp.json` 和 `scripts/inkflow-mcp.ps1`；如果编辑器没有自动发现服务，重新加载工作区即可。

## 公开篇章判断单

当前篇章全部进入正史后，先说“给我下一篇的公开判断单”。Writer 会生成 `planning/arc_*.md`：其中写明这篇要回答的核心问题、正史约束、为什么选这条升级路径、每章大致转折/钩子和仍需核对的风险。

这不是模型的原始逐步思维链；墨流不会保存或展示供应商 `reasoning_content`。判断单不改 `PLAN.md`、正文或 SQLite。用户可以看完后说“按这份判断单展开下一篇章节卡”，此时正式规划会把它作为输入之一；也可以自然地指出要调整的方向。

当前 MVP 打通以下闭环：

```text
创建小说项目
→ 生成全书/卷/篇章/章节卡四级规划
→ 构建唯一 Context Packet
→ 写作 Agent 生成章节草稿
→ 审查 Agent 输出证据化报告
→ 若需修改，写作 Agent 读取旧稿和同版本审查报告并定点修订
→ 重新审查当前版本
→ 用户验收
→ 记忆 Agent 提取状态变化
→ 若有歧义，仅限一次证据化自解析；仍冲突则输出用户可见报告并停止
→ SQLite 事务提交并更新 STATE.md
→ 自动创建 SQLite/Markdown 一致检查点
→ 当前篇章完成后，依据已接受正史滚动细化下一篇章
```

## 普通用户使用方式

1. 用支持 Agent 与 MCP 的编辑器打开本文件夹。
2. 按上面的安装步骤完成依赖与 Key 配置，让编辑器 Agent 执行“连接墨流并创建小说项目”。
3. 在安全输入框中配置 DeepSeek Key。Key 只进入环境变量或 Windows Credential Manager，不进入小说文件、Git 或 Obsidian。
4. 直接说：“创建一本都市悬疑小说，主角是……”“规划第一卷”“写第一章并审查”。

项目内的 `.mcp.json`、`.vscode/mcp.json`、`CLAUDE.md`、`AGENTS.md`、`.github/agents/` 和 `.trae/rules/` 为不同宿主提供同一套工作流。

## 终端自然语言会话

如果你希望不经过编辑器 Agent，也可以直接在终端里用自然语言驱动同一套门禁：

```powershell
.\.venv\Scripts\inkflow.exe chat "C:\\你的小说项目"
```

在 `墨流>` 后可以输入：

- 不需要背固定指令；“帮我瞅瞅现在写到哪儿了”“行，就照刚才那个办法弄”也会按整句语义理解；
- “把第 7～11 章的章节规划一次给我看，我看完后统一确认”；
- “根据审查意见修订第 1 章，复审；仅通过才接受”；
- “当前篇章完成后规划下一个篇章”；
- “按复审结论修正这批草稿的第 9 到第 10 章，保留批次但不要接收”；
- “继续完整三 Agent 流程，直到已接受正文达到 10 万汉字”；
- “预览回退到生成第 2 章之前会影响哪些文件”。

会话层只路由至既有 Writer、Reviewer、Memory Keeper 与确定性恢复工作流；它不会自行写正文、直改 `.inkflow` 或使用 `force`。长跑只统计 accepted 正文，按正史字符目标或小说门禁停止；不会自动查询余额或因余额停止。`balance` / `novel_provider_balance` 仍可由用户主动调用，作为普通账户信息查询。

路由采用“宽松理解、严格执行”：低风险且信息完整时直接做；存在真实歧义时只问缺少的部分；进入正史、改变未来规划或执行回退时仍保留对应门禁。模型漏掉明确的章节数字时，本地程序会从原句复制“第 N 章/第 N 到 M 章”，但不会用关键词猜测用户权限。

询问频率控制的是“可选的创作确认”，不是要求用户背触发词：低档只补必需信息，中档在方向明显不确定时询问，高档会更积极核对关键选择，超高档倾向每个有意义的分岔先问一句。用户当次说“直接开始，不用再问”会覆盖可选询问；涉及正史、回退或不可逆影响的必要确认仍会保留。界面展示模型答复、公开判断、约束和工具状态，但不展示或保存供应商的原始思维链。

## 轻量会话升级说明

本次升级不迁移 SQLite，也不改写 `BOOK.md`、`PLAN.md`、`STATE.md` 或已有章节。已有小说项目第一次使用 `inkflow chat` 进行讨论或执行后，会在项目根目录新增一个可阅读、可删除的 `DIALOGUE.md`；它最多保留 10 轮用户可见讨论、确认答复和已路由动作，不保存模型原始推理或 API Key。

普通写作不再隐式执行高消耗审查：

- “写第 3 章草稿，先给我看”只调用 Writer；
- “按意见修订第 3 章，先给我看”只调用 Writer 修订；
- “审查第 3 章”只调用 Reviewer；
- “审查通过后验收第 3 章并入正史”才会继续调用 Memory Keeper；
- “连续写到 10 万字”仍是完整门禁长跑，不会额外插入余额查询。

会话主控只理解讨论和确认，不能写正文、审查正文、直接修改 `.inkflow` 或绕过 `force=false`。这不是新增的小说 Agent，Writer、Reviewer、Memory Keeper 的职责边界保持不变。

规划完成后，`PLAN.md` 会显示一段“公开推理摘要”和后续核对点：它说明 Writer 已检查的约束、选择当前篇章走向的可复核依据和仍未解决的风险。它不是供应商原始思维链；原始推理不保存、不进入正史，也不会被下一章当作事实。

一次查看多章规划时，墨流使用只读批次预览：集中返回 1～20 张已有章节卡，不调用模型、不修改规划。用户可以一次确认全部，也可以只说“第 8、10 章需要调整”，无需逐章停住等待。

## 批量草稿、篇章复审与评分

“生成第 9 到第 16 章批量草稿”会逐章执行 Writer → 即时 Reviewer →（必要时独立 Writer 修订 → 重审），但只写入 `batches/` 与 `chapters/*.draft.md`，不会进入 SQLite 正史。批次完成后可说“复审第 9 到第 16 章并和规划对比”，得到 `reviews/arc_audit_*.md`。

篇章复审把问题明确分开：临时正文的问题可在原批次内“修订 → 逐章重审 → 同步批次清单”，不必重写无关章节；未来规划的问题必须由用户确认后才会重规划；已接受正文的问题必须先预览影响并在分支中重新执行 Writer → Reviewer → Memory Keeper，不能只改 Markdown 或只补记忆。

每份章节审查还有透明 100 分制：正史与认知 25、因果与人物 25、章节卡履约 20、完整性 15、表达与节奏 15。扣分只能来自报告中已列出的证据；即使结论 `pass`，所有未满分项目也会写明具体扣分原因和依据。分数解释质量，不取代 `major`/`blocking` 的正史门禁。

## 升级已有小说项目

升级到 0.2 **不迁移正史数据库 `.inkflow/inkflow.db`**。桌面版会自动创建或增量补齐独立的 `.inkflow/studio.db`，只保存文档版本、批注、手工故事圣经、场景笔记和任务记录；删掉它不会删除正史，但会失去这些桌面辅助记录。更新源码后，在工作区执行一次：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

然后直接用原项目继续会话即可；`DIALOGUE.md` 会在第一次会话时按需创建，已有 `BOOK.md`、`PLAN.md`、`STATE.md`、章节及 `.inkflow/inkflow.db` 不会仅因升级而被改写。将来若确实需要正史数据库迁移，墨流会先自动创建不可变检查点、显示受影响文件与数据库版本、要求确认，再执行可回退迁移；不会静默改造已有小说。

## 番茄公开页参考适配器

`novel_reference_fetch_fanqie` 可导入无需登录的 `fanqienovel.com` 公开页面，并使用既有 `novel_reference_analyze` 生成节奏特征卡。它不下载受限内容、不处理登录，也不绕过访问控制。页面返回动态空壳时会给出可行动诊断；届时可导入用户有权使用的 TXT/MD，或以后选择安装可选本地浏览器后端。该后端当前未安装，因此没有额外服务或费用。

## 开发者快速验证

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

在线请求需要 `DEEPSEEK_API_KEY`，或者先运行 `python -m inkflow.cli configure-key` 把 Key 保存到系统凭据库。离线单元测试使用脚本化模型，不消耗 API Token。

真实在线闭环烟雾测试必须显式确认，以免误耗 API Token：

```powershell
.\.venv\Scripts\python.exe .\scripts\online_mvp_smoke.py --live
```

生成 Windows 安装包、VSIX 和各自携带的本地引擎：

```powershell
.\scripts\build-release.ps1
```

构建结果位于 `desktop/release/` 与 `vscode-extension/release/`。完整的 0.2 能力、升级影响和已知边界见 [0.2 发布说明](./docs/V0.2_RELEASE_NOTES.md)。

总体设计见 [墨流-InkFlow_长篇网文Agent总体设计方案.md](./墨流-InkFlow_长篇网文Agent总体设计方案.md)。开发者自己的 Obsidian 知识库和本地研究缓存不会上传到 GitHub，也不是运行依赖。

## 数据与安全边界

- API Key 只从环境变量或系统 keyring 读取；仓库不保存真实 Key。
- SQLite 是正史，Markdown 是用户可读投影；不要直接编辑 `.inkflow/inkflow.db`。
- 草稿、审查、运行 Trace 和小说数据库默认留在用户指定的本地小说项目目录。
- 番茄适配器只读取无需登录的公开页面，不绕过登录、付费或访问控制。
- 墨流展示公开判断摘要、证据、工具状态和用量，不保存供应商的原始逐步思维链。

## 许可证

代码以 MIT License 发布。使用外部模型和参考内容时，仍需遵守相应服务条款与内容来源规则。
