# 墨流（InkFlow）长篇网文 Agent 总体设计方案

> 文档状态：架构规划稿 v0.3（编辑器原生版）
>
> 目标读者：产品设计者、专业 AI 写手、后续开发人员
>
> 首发形态：可直接交给 Claude Code、VS Code Agent/Copilot、TRAE、Codex 等编程 Agent 打开的小说工作区；本地 Python 服务与 EXE 只是后台能力，不要求普通用户操作终端
>
> 默认模型基线：DeepSeek 官方 `deepseek-v4-flash`，同时保留 OpenAI-compatible、Anthropic-compatible 与本地模型适配层
> 默认创作范围：中文网文，单章 2000～4000 字，长篇可持续写作 200～500 章以上

## 1. 产品结论

InkFlow 不应做成三个大模型在群聊里互相讨论的系统。自由群聊看起来像“多 Agent”，实际容易重复调用、互相污染上下文、产生无人负责的事实修改，也很难在断网或崩溃后恢复。

本系统采用“一个确定性引擎、三个专业 Agent、一个唯一事实库、一个对模型可见的上下文包”的结构。用户面对的是编辑器里的自然语言对话、文件差异和可折叠过程面板，不是命令行：

```text
Claude Code / VS Code / TRAE / Codex 中的自然语言
              │
              ▼
 工作区规则 + MCP 工具层 + 对话意图解析器
              │
              ▼
      确定性 Novel Engine
              │
   ┌──────────┼──────────┐
   ▼          ▼          ▼
写作 Agent  审查 Agent  记忆 Agent
   │          │          │
   └──────────┼──────────┘
              ▼
 SQLite 时态事实库 + Qdrant 派生索引
              │
              ▼
 BOOK.md / PLAN.md / STATE.md / 章节 MD / 审查 MD
```

Novel Engine 不创作，也不进行文学判断。它只负责状态迁移、权限、重试、检查点、成本、用户验收和事务提交。因此产品仍然是三个 Agent，而不是四个 Agent。

三个 Agent 的边界必须固定：

- 写作 Agent 负责思考、规划、正文和局部修改，但只能读取小说事实，不能直接把自己写出的新设定塞进事实库。
- 审查 Agent 负责带证据的质量检查，只能提交问题清单与修改建议，不能越权更新正史。
- 记忆 Agent 是唯一能提出事实变化并提交数据库的 Agent。它不能替作者补写正文里没有发生的事件。

正常章节流程为：

```text
PREPARE_CONTEXT
→ THINK_AND_PLAN
→ DRAFT
→ AUDIT
→ USER_REVIEW
→ PATCH（可选，最多两轮）
→ MEMORY_PROPOSE
→ MEMORY_VALIDATE
→ ATOMIC_COMMIT
→ INDEX
→ WAIT_OR_NEXT
```

默认在 `USER_REVIEW` 停下，把正文、审查意见和文件差异显示在用户正在使用的编程工具中。用户可以在对话里说“接受”“重写第三场”“保留正文但暂不入库”，也可以直接编辑 MD。自动连续写作仍然保留，但不是大众用户的默认行为。

## 2. 用户定位与交互原则

产品面向两类人：一类是会使用 Claude Code、VS Code、TRAE、Codex 等编程 Agent，但不想研究小说系统内部参数的创作者；另一类是会调整 Prompt、模型、检索和审查策略的专业 AI 写手。两类用户都只需“打开小说文件夹并对话”，不能要求他们先进入终端记忆命令，也不能要求第一类用户理解 temperature、RRF、embedding、reranker 或 reasoning effort。

因此界面只暴露四个创作预设：

| 预设 | 适用情况 | 系统行为 |
| --- | --- | --- |
| 专业均衡 | 默认 | 全流程 Thinking，256K 上下文预算，逐章人工验收 |
| 稳健连载 | 已写较长、设定复杂 | 384K 上下文，更严格连续性审查，较少候选方案 |
| 灵感探索 | 开书、转折、卡文 | 生成多个短方案，增加结构差异，但不放松事实门禁 |
| 节省模式 | 低预算试写 | 192K 上下文，减少候选与专项审查，仍保留记忆事务 |

普通用户首次把文件夹交给编程 Agent 后，只需要回答：题材、目标读者、主角、核心卖点、预计总量、单章字数、是否逐章验收。Agent 调用工作区的初始化工具，自动生成书级方向、卷纲、当前篇章细纲和首批章节卡。模型参数放在高级配置里，并由模型能力探测器自动纠正无效组合。

系统还需要基本聊天意识。用户可以直接说：

- “先别写，和我讨论一下男主为什么会背叛。”
- “第十二章的战斗太快，把失败后的余波写重一点。”
- “以后少用每个人都意味深长地笑这种句子。”
- “这一卷写到三十五章，写完再停。”
- “把女主知道真相的时间推迟五章，但不要修改已经发布的章节。”

对话外壳先判断这是咨询、创作、修改、运行控制还是正史变更。普通讨论写入会话记忆；用户偏好写入候选偏好；涉及人物、世界、时间线和大纲的修改先生成 `change_proposal.md`，用户确认后才进入正史。这样既有聊天感，也不会因为一句随口讨论破坏整本书。

## 3. 对参考项目的细化结论

### 3.1 对现有 `novel_agent` 的继承与替换

现有项目已经有七类真相文件、热/温/冷记忆、Writer、Auditor、Revisor、Style Learner、题材库和自然语言入口。这些不是应当丢掉的旧代码，而是新系统的需求来源。

参考项目：[littlewinter320/novel_agent](https://github.com/littlewinter320/novel_agent)

应保留的设计：

- 人物知识边界、资源流、时间线、世界规则必须交叉验证。
- 当前会话、跨会话偏好、历史章节需要不同记忆层级。
- 写作后必须审查和修订，不能把模型第一次输出直接当成成品。
- 题材知识、对话风格、表达变体和用户修改记录应形成长期资产。
- 用户应能用自然语言控制 Agent，而不只靠命令行参数。

应替换的实现：

1. 冷记忆目前按 JSON 文件和关键词线性扫描，章节增多后召回率和速度都会下降。新系统改为 SQLite 精确事实查询、BM25 关键词召回和向量语义召回。
2. 真相文件分散写入时缺少真正事务。新系统用数据库事务一次提交章节版本、事实变化、摘要和检查点。
3. 当前审查将十多个维度拆成多次大模型调用，一些检查只读取正文前 2000～3000 字，无法判断章节后半段；异常路径还可能返回通过。新系统改为确定性规则检查一次、综合模型审查一次、高风险专项检查可选一次，任何异常只能是 `UNKNOWN` 或 `RETRY`，不能是 `PASS`。
4. Revisor 作为独立第四创作角色会增加上下文转换。新系统把局部修订收回写作 Agent 的 `PATCH` 模式，维持三个 Agent 的责任闭环。
5. 当前模型客户端只处理通用温度和输出长度，无法正确表达 Thinking、工具调用、JSON Schema、上下文能力和不同供应商的参数差异。新系统需要能力驱动的 Provider Adapter。

### 3.2 对 `ainovel-cli` 的采用

`ainovel-cli` 最值得采用的不是 Go 或命令行界面，而是“事实层确定、语义层自主”：可枚举的流程交给代码，开放式创作交给模型，语义裁决输出结构化结果。它还证明了长篇项目需要检查点、原子文件替换、待提交恢复、滚动规划、分层摘要和逐章验收门。

InkFlow 会采用以下做法：

- Agent 之间不直接聊天，通过数据库工件协作。
- 每个步骤有单调递增检查点和内容摘要哈希。
- 相同输入与相同步骤重复执行时可以幂等返回已有产物。
- 连续出现相同任务但事实没有推进时计为僵局，三次后升级处理，五次后停止等待用户。
- 章节许可与具体章节号绑定。崩溃恢复不能把第十二章的许可错误用于第十三章。
- 长篇详细规划当前篇章内的全部章节卡，下一到两个篇章保留中等粒度，更远的卷只保留骨架，避免一次性把数百章后期写死。

参考项目：[ainovel-cli](https://github.com/voocel/ainovel-cli)

### 3.3 对 `AI-Novel-Writing-Assistant` 的采用

该项目的混合 RAG、章节任务、事实账本、人物资源账本、伏笔兑现账本和检索轨迹值得参考。它的 RAG 默认将内容切块后进行向量与关键词候选召回，再融合和精排，这比单纯向量搜索适合专有名词很多的中文小说。

InkFlow 不直接复制其代码，而是采用以下概念：

- 一条检索结果必须知道自己来自哪个章节、人物、事实、伏笔或参考资料。
- 检索过程要落盘，便于回答“为什么模型看到这段内容却没看到另一段”。
- 未兑现的重要线索不因年代久远而简单降权。
- Qdrant 是可重建索引，不是正史数据库。
- 章节生成后，要把状态、摘要和伏笔变化同步回事实层。

需注意该项目默认采用 AGPL 并含商业授权要求，现阶段只参考思想，不复制闭源商业产品中的实现代码。[项目与许可证](https://github.com/ExplosiveCoderflome/AI-Novel-Writing-Assistant)

### 3.4 对 Humanizer-zh 与 HumanWriting 的采用

Humanizer 不作为“检测 AI 率”的独立 Agent，也不承诺绕过检测器。它进入写作契约、审查规则和文风统计器，重点识别模式簇：过度解释、同义词轮换、装饰性排比、段尾总结、口号式提升、固定转折、角色同声、每章同类悬念结尾等。

系统不能把词语做成永久黑名单。人物有意重复口头禅、某类叙述者偏爱长句、某段情绪需要排比时，应允许保留。判断对象是作品在连续五章、二十章中的分布，不是某个句子是否“像 AI”。

参考项目：[Humanizer-zh](https://github.com/op7418/Humanizer-zh)

### 3.5 产品外壳：编程 Agent 工作区，而不是终端应用

真正的产品交付物不是一串 PowerShell 命令，而是一个能被主流编程 Agent 直接理解的项目文件夹。Python 只承载确定性引擎、数据库、检索和爬虫；MCP 是跨客户端的稳定能力接口；各编辑器的规则文件负责告诉宿主 Agent 何时调用这些能力。

首版工作区适配层如下：

| 宿主 | 面向宿主的文件 | 作用 |
| --- | --- | --- |
| 通用/Codex | `AGENTS.md` | 项目范围、三 Agent 边界、写章流程、禁止绕过记忆提交 |
| Claude Code | `CLAUDE.md`、`.claude/skills/inkflow/SKILL.md`、项目 MCP 配置 | 把“规划、写章、审查、接受、查正史”变成自然语言可触发流程 |
| VS Code/Copilot | `.github/copilot-instructions.md`、`.github/agents/*.agent.md`、`.github/prompts/*.prompt.md`、工作区 MCP 配置 | 提供三个角色入口、可复用提示和工具权限 |
| TRAE | 由 `bootstrap` 按已安装版本生成的项目规则与 MCP 配置 | 不把可能变化的目录写死在核心；安装时探测版本并套用模板 |
| 其他支持 MCP 的客户端 | `mcp` 服务清单＋`AGENTS.md` | 至少获得完整工具能力和通用操作契约 |

用户打开目录后可以直接说“先规划第一卷”“展开第二篇章的 15 章细纲”“按照第 12 章章节卡写草稿”“审查并把问题折叠显示”。宿主 Agent 只负责理解对话和呈现结果，不直接读写 SQLite，也不自己拼小说上下文。所有正式动作都走带 Schema 的 MCP 工具；这样换 Claude、GPT、DeepSeek 或宿主编辑器时，小说状态机不会换一套。

安装仍保留 `InkFlow.exe` 和 Python 入口，但它们是后台服务、双击初始化器和开发诊断面，不是普通用户的主交互面。初始化器负责检测可用编辑器、生成适配文件、注册项目 MCP、检查模型连接并创建示例工作区；完成后用户回到编辑器对话即可。

适配实现以官方文档为准并做版本化模板，优先参考 [Claude Code MCP](https://code.claude.com/docs/en/mcp)、[VS Code MCP servers](https://code.visualstudio.com/docs/copilot/customization/mcp-servers)、[VS Code custom agents](https://code.visualstudio.com/docs/copilot/customization/custom-agents) 与 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。TRAE 的项目规则和 MCP 文件由安装时能力探测生成，避免用未经确认的固定路径误导用户。

### 3.6 四级规划：书、卷、篇章、章节

此前只保留书罗盘、卷纲和未来几章滚动计划还不够。长篇网文需要一个能逐层展开、又不会一次写死数百章的四级规划树。规划仍由写作 Agent 的 `PLAN` 模式完成，不增加第四个“规划 Agent”。

```text
全书罗盘 Book Compass
└─ 卷计划 Volume Plan
   └─ 篇章/故事弧 Arc Plan
      └─ 章节卡 Chapter Card
```

**全书罗盘**回答“这本书最终给读者什么”：一句话 premise、核心卖点、目标读者、长期叙事发动机、主冲突、主角起终点、主题问题、结局方向、预计卷数/章节数，以及不能破坏的读者承诺。它是方向盘，不写死中后期每个事件。

**卷计划**回答“这一卷怎样完成一个阶段性承诺”：卷首状态、卷末状态、本卷目标、对手压力、升级或关系曲线、中段转折、卷末高潮、代价与结果、包含哪些篇章、预计章节区间，以及通往下一卷的桥。

**篇章计划**是 8～30 章左右的可执行故事弧，长度允许按题材调整。每个篇章必须记录：

- 篇章承诺、中心矛盾、开始状态和结束状态；
- 主角想达成什么、阻力怎样逐段升级、失败代价如何兑现；
- 关键揭示、错误认知、关系变化、成长/奖励周期；
- 本篇要种下、推进、延期和兑现的伏笔与读者承诺；
- 入口事件、中段不可逆转折、高潮、余波、通向下一篇的桥；
- 预计章节数、允许伸缩范围和“何时必须重新规划”的触发器。

**章节卡**覆盖当前篇章内的每一章。最少字段为：

```yaml
chapter: 12
title_working: 雨夜的错误胜利
status: planned
pov: 林照
time_location: 第九日深夜 / 南仓
function: 发现 + 关系反转
goal: 在巡查前拿到账本
obstacle: 账本是诱饵，盟友还隐瞒了一层计划
decision: 林照选择先救盟友而不是销毁证据
consequence: 身份暴露，但得到半页真账
irreversible_delta: 反派确认内鬼就在三人之中
scenes:
  - 潜入与异常安静
  - 发现诱饵并争执
  - 追捕中的选择与代价
information_release: 只揭示账本被替换，不揭示替换者
foreshadow_advance: thread:missing_seal
payoff: promise:chapter_07_warning
hook_type: 新危险 + 二选一
hook_question: 门外喊出林照真名的人是谁？
target_words: 3200
dependencies: [fact:warehouse_route, belief:linzhao_not_know_traitor]
```

章节钩子不能等同于“每章最后突然有人敲门”。系统使用问题、危险、揭示、艰难决定、逆转、代价、倒计时、关系破裂、错误胜利、新目标等钩子类型做软轮换；允许某些章节以余韵、情绪落点或阶段完成感收尾。审查 Agent 检查连续三至五章的钩子重复率、承诺是否兑现、章节功能是否同质化，而不是机械要求每章悬崖式中断。

规划采用“近处精细、远处模糊”的伸缩策略：全书有罗盘；当前卷有完整卷纲；当前篇章展开全部章节卡；下一到两个篇章保留中等粒度；更远的卷只保留阶段承诺。每写完一章只允许局部校正章节卡，每到篇章边界进行一次再规划，每到卷末才重新评估全书路线。这样既避免无大纲崩坏，也避免模型被几百章伪精确计划绑死。

计划门禁顺序固定为：`书罗盘存在 → 当前卷存在 → 当前篇章已展开 → 当前章节卡通过校验 → 才能写正文`。校验器检查章节区间重叠、因果依赖、承诺/兑现覆盖、连续功能重复、未结线索数量和篇章结束状态。用户可直接编辑 `PLAN.md`；文件监视器把改动解析为计划补丁，显示影响范围，确认后再进入数据库。

## 4. 三个 Agent 的详细契约

### 4.1 写作 Agent

写作 Agent 有四个运行模式：`CONSULT`、`PLAN`、`DRAFT`、`PATCH`。其中 `PLAN` 内部再分 `BOOK`、`VOLUME`、`ARC`、`CHAPTER` 四个作用域，但仍是同一个 Agent、同一套事实读取权限。

`CONSULT` 用于和用户讨论剧情，不产生正式章节。`PLAN` 生成章节执行契约，`DRAFT` 按契约生成正文，`PATCH` 只修改审查指定区间。每次写作前必须先读取上下文包和未解决问题，不允许从聊天历史猜测正史。

`DRAFT` 前不再临时凭空生成计划，而是读取已经通过门禁的章节卡，并把它补成一次性的章节执行契约。该契约至少包括：

- POV 人物、时间、地点和场景边界；
- 人物当前欲望、阻力、选择及选择代价；
- 本章必须发生的事实；
- 本章禁止提前泄露的信息；
- 需要推进的主线、支线、人物线和感情线；
- 需要种下、推进或兑现的伏笔；
- 章节功能，如行动、发现、关系、余波、失败或转折；
- 章节结束后的不可逆状态变化；
- 推荐字数范围和段落节奏；
- 近五章重复风险与本章应主动变化的表达方式。

为避免 DeepSeek 在高思考模式下写得过于工整，创造性不依赖温度，而使用“创意镜头卡”：同一个核心事件可随机选择误解、延迟信息、错误胜利、代价交换、关系逆转、空间限制、物件触发、旁观者压力等不同镜头。重要章节先生成三个 200～400 字的微方案，再由同一 Agent 根据新颖度、人物合理性、因果和重复度选择。只对微方案多采样，不生成三篇完整正文。

正文文件格式：

```text
chapters/chapter_001.draft.md
.inkflow/runs/<run_id>/chapter_001.meta.json
```

正文 MD 只保留可阅读内容；模型用量、上下文来源、提示版本、模型名、哈希放在隐藏的运行记录中，避免污染用户看得到的作品目录。

### 4.2 审查 Agent

审查 Agent 接收完整正文、章节契约、硬事实包、前章结尾、相关历史证据和文风统计。正常只调用一次模型，返回 JSON Schema：

```json
{
  "verdict": "pass|patch|replan|unknown",
  "confidence": 0.0,
  "findings": [
    {
      "category": "timeline|character|knowledge|world|causality|pacing|style|originality",
      "severity": "info|minor|major|blocking",
      "chapter_span": {"start": 0, "end": 0},
      "evidence": "正文中的短证据",
      "canon_refs": ["fact:...", "chapter:..."],
      "explanation": "为什么构成问题",
      "repair_instruction": "只描述应改什么，不直接重写整章"
    }
  ]
}
```

审查分三层：

1. 代码层：字数、人物名误写、重复段、异常符号、章节标题、禁用内容、时间数值、物品负数、数据库唯一约束。
2. 模型层：人物动机、因果、认知边界、承诺兑现、节奏、文风和读者体验。
3. 风险层：时间穿越、悬疑泄密、复杂战力或卷末收束时追加专项审查。

审查报告同时写出人类可读文件：

```text
projects/<book_id>/reviews/chapter_001_review.md
projects/<book_id>/reviews/chapter_001_review.json
```

用户只看 MD 就能决定，不需要阅读 JSON。任何模型超时、非法 JSON、缺失完整正文或检索证据不足，都必须标为 `unknown` 并停止提交。

### 4.3 记忆 Agent

记忆 Agent 只处理已经通过审查并由用户接受的版本。它不总结整本书，而是提取本章相对于上一检查点的差异：

- 新事件与事件顺序；
- 人物身体、心理、目标、位置和能力变化；
- 人物知道、怀疑、误解或隐藏的内容；
- 关系强度和关系性质变化；
- 物品、资源、债务、伤势和权限变化；
- 新规则、规则例外和规则被证伪；
- 伏笔种下、推进、兑现或延期；
- 故事承诺的进度；
- 场景摘要、章节摘要和文风统计。

每条候选事实必须带正文来源区间、置信度和事实性质。数据库验证器检查时间倒退、重复实体、互斥状态、知识提前、物品重复持有和旧事实覆盖关系。无法自动判断的冲突写入 `memory_conflicts.md` 等待用户，不擅自选择。

提交使用两阶段流程：

```text
memory_patch.json（候选）
→ schema_validate
→ conflict_validate
→ SQLite transaction
→ chapter accepted version
→ checkpoint
→ 异步 Qdrant 索引
```

Qdrant 失败只标记索引待同步；SQLite 事务失败则整次提交回滚，草稿仍在，不丢内容。

## 5. 全 Thinking 与多模型适配

### 5.1 默认推理策略

按照产品要求，所有 LLM 工作默认开启 Thinking。不同任务使用不同 effort，而不是简单把全部请求设成最大：

| 工作 | 默认 effort | 升级条件 |
| --- | --- | --- |
| 普通聊天、意图解析 | low | 涉及正史修改时升 high |
| 章节规划 | high | 卷末、复杂反转升 max |
| 正文生成 | high | 默认不升 max，防止输出过度解释 |
| 综合审查 | high | 发现高风险矛盾后升 max 专审 |
| 记忆提取 | high | 多条冲突或回溯修订升 max |
| 卷末连续性审查 | max | 固定使用 max |

DeepSeek 官方文档显示 `deepseek-v4-flash` 当前支持 1M 上下文、Thinking/Non-thinking、JSON 和工具调用。Thinking 默认开启且默认 effort 为 high；`low/high/max` 可控。Thinking 模式会忽略 `temperature`、`top_p`、`presence_penalty` 和 `frequency_penalty`，所以界面不应显示一组看似可调但实际无效的旋钮。[DeepSeek 模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)

系统始终展示可审计的过程摘要：阶段计划、关键取舍、证据引用、工具/Skill、文件差异和最终决定。若供应商明确返回 `reasoning_content`，可按用户设置在折叠区显示并短期保存；但它不进入小说正史，也不被塞进下一章上下文。这样既让用户看见 Agent 的工作过程，又不让大量未经验证的推理文本污染长期记忆。使用工具调用时，Provider Adapter 仍须按 DeepSeek 协议在同一轮工具链中正确回传该字段。

### 5.2 更高的上下文预算

之前 80K～160K 对目标产品偏保守。新默认如下：

| 场景 | 软预算 | 硬上限 | 输出预留 |
| --- | ---: | ---: | ---: |
| 普通章节 | 256K | 320K | 16K～24K |
| 设定密集或多线章节 | 384K | 448K | 24K～32K |
| 卷末/重大反转审查 | 512K | 576K | 32K |
| 节省模式 | 192K | 256K | 12K～16K |

这里是 Token 预算，不是必须填满。上下文构建器先按优先级装载，达到软预算后停止软资料；硬事实、用户本轮指令和输出预留永不被挤掉。超过硬上限时先删除低相关参考资料，再压缩旧章节摘要，最后才请求用户选择，不能静默截断正文。

上下文摆放采用“首尾加权”：固定契约和关键硬事实放前部；当前章节契约、前章尾部和用户指令放最后；低优先级参考特征放中部。这样减少超长上下文中的中段遗忘。

### 5.3 多模型能力探测

模型不是写死为 DeepSeek。Provider Adapter 首次连接时自动检测：

- 最大上下文和最大输出；
- 是否支持 Thinking 及 effort 档位；
- 是否支持 JSON Schema、普通 JSON、工具调用和流式输出；
- Thinking 时哪些采样参数无效；
- 是否返回用量、缓存命中和 reasoning token；
- 工具调用多轮是否需要回传推理字段；
- 429、超时、断流和内容过长的错误形态。

用户只选供应商、模型并输入 Key。系统生成 `provider_capabilities.json`，再把专业均衡预设映射到实际能力。模型不支持 Thinking 时，退化为“先结构化分析、再生成最终答案”的双调用软推理；不支持 JSON Schema 时，用 JSON 修复器和 Pydantic 验证；不支持工具调用时，由引擎把检索结果直接注入。

第一版不做模型权重微调。先做提示契约、示例库、检索、用户反馈学习和离线评测这类“软微调”。当积累足够多的用户接受/拒绝样本后，再考虑对开源模型做 LoRA；不能拿少量小说直接把模型调成僵硬模板。

## 6. Novel-RAG：前后逻辑速通方案

RAG 不只是一个向量库，也不应表现成十几个摘要文件让模型自己翻。InkFlow 内部可以有五条检索通道、许多表和派生摘要，但每次模型调用只接收一个由 `novel.context.build` 生成的 **Context Packet**。Agent 不允许递归浏览 `.inkflow`、自行挑摘要或把整个工作区塞进提示词。

Context Packet 使用稳定顺序：

```text
A. 当前任务与用户本轮要求
B. 不可违反的硬正史
C. 本章所在的书/卷/篇章/章节规划切片
D. 当前登场人物状态与各自知识边界
E. 前章结尾和最近相关章节
F. 历史证据（带章节、场景、事实 ID）
G. 未结伏笔、故事承诺、钩子与本章兑现义务
H. 当前任务真正相关的参考作品特征卡
I. 用户偏好、文风契约和禁令
J. 输出格式、验收条件与剩余 Token 预算
```

这解决了“摘要文件越多，模型越容易搞混”的核心问题：复杂度留在确定性上下文构建器中，模型只看到一个有优先级、有来源 ID、有 Token 预算的任务包。包内出现冲突时以硬正史为准；无法消解则标记 `CONTEXT_CONFLICT` 并停止写作，不能让模型自行投票。

Packet 有 `compact`、`standard`、`deep` 三档。正常章节先用 256K 上限下的 `standard`；高复杂章节可扩展到 384K；卷末或全局回溯最多 512K。这里的上限是可用容量而非填充目标：一个 60K 就足够的章节不会为了“用满 256K”而加载无关摘要。

内部五条检索通道如下：

### 6.1 Canon RAG：正史硬事实

来源是 SQLite，不走模糊向量判断。查询当前人物状态、知识边界、时间地点、物品、关系、规则、未兑现伏笔和用户禁令。结果带稳定 ID，优先级最高，参考作品不能覆盖它。

### 6.2 Narrative RAG：本书历史叙事

来源是已接受章节、场景摘要、章节摘要、故事弧摘要和卷摘要。使用 Qdrant 的 dense＋BM25 召回，按小说、章节范围、人物、地点、线索、POV 和故事线过滤，再用 RRF 融合及 reranker 精排。

### 6.3 Reference RAG：参考作品特征

只保存经过分析的抽象特征卡，例如开篇承诺、升级周期、冲突密度、章末钩子类型、人物关系推进、信息释放速度、对话比例和读者奖励方式。默认不把参考作品长段原文送入写作 Agent。

### 6.4 Craft RAG：写作知识

保存题材知识、叙事技巧、HumanWriting 规则、常见失误、平台节奏和经过验证的技巧卡。它提供方法，不提供要照抄的句子。

### 6.5 Preference RAG：用户偏好

保存用户明确确认的偏好、修改对比和拒绝原因。偏好有作用域：全局、某本书、某人物、某一卷或临时会话。临时抱怨不能自动成为永久规则。

### 6.6 检索流程

```text
章节契约生成查询意图
→ SQL 精确取硬事实
→ Dense 召回 40 条
→ BM25 召回 40 条
→ 元数据过滤
→ RRF 融合
→ 时态与叙事距离调整
→ reranker 重排前 24 条
→ 多样性去重
→ 选 8～14 条证据
→ 按 Token 预算装配
→ 保存 retrieval_trace.json
```

中文嵌入建议以 BGE-M3 为默认，精排使用 `bge-reranker-v2-m3`；桌面低配置环境允许先关闭本地 reranker，保留 BM25 和向量融合。参考：[Qdrant 混合检索](https://qdrant.tech/documentation/advanced-tutorials/reranking-hybrid-search/)、[BGE-M3](https://arxiv.org/abs/2402.03216)

切块不能统一固定为 800 字：

- 自己的章节按场景边界切，目标 800～1600 汉字，重叠 100～180 字。
- 对话密集场景按完整对话轮次边界切，不在一句话中间截断。
- 世界设定和人物卡按字段切，不按字数切。
- 章节、篇章和卷摘要保存为 SQLite 派生记录并独立索引，而不是散落成模型可任意读取的一堆文件。
- 参考文本原始归档不直接进入生成索引，先生成 300～800 字的特征卡。

系统提供 `novel.context.build(mode="continuity")` 工具生成“前后逻辑速通包”：当前时间线、人物位置、知识边界、未结伏笔、主支线进度、最近状态变化、潜在矛盾和相关原文证据。需要人工查看时再导出为临时 MD；写作 Agent 和审查 Agent 始终读取同一份结构化 Packet，避免各自理解不同版本。

## 7. 时态事实数据库

SQLite 是唯一事实源，建议核心表如下：

```text
books
book_contracts
style_contracts
chapters
chapter_versions
scenes
entities
entity_aliases
facts
events
timeline_edges
character_states
character_beliefs
relationships
items_and_resources
world_rules
plot_threads
story_promises
foreshadows
summaries
user_preferences
audit_runs
audit_findings
memory_patches
checkpoints
model_runs
retrieval_traces
reference_sources
reference_documents
reference_feature_cards
index_jobs
```

`facts` 采用时态字段：`valid_from_chapter`、`valid_to_chapter`、`supersedes_fact_id`、`truth_status`、`source_span`。例如“角色在杭州”不能覆盖历史记录，而是在新章节关闭旧事实并新建有效区间。

人物认知必须单独存储：

```text
character_id
proposition_id
belief_status: knows / suspects / believes_false / unknown
learned_at_chapter
source_event_id
```

这能避免悬疑、谍战、宫斗和多 POV 小说中最常见的“角色提前知道答案”。

数据库启用 WAL、外键和事务。每次提交生成 `commit_id`，数据库迁移使用 Alembic。Qdrant 中所有向量都带 `accepted_chapter_version_id`，草稿和被拒版本不能被后续章节检索到。

## 8. 爆款小说识别与参考作品应用

### 8.1 对现有番茄爬虫的真实评价

本地页面 `http://localhost:3000/projects/fanqie-crawler` 与它提供的新版源码已完成检查。新版实现比旧版单文件脚本可靠：

- `fetcher.py` 使用域名白名单、robots.txt、至少一秒间隔、同一 Session、SHA-256 缓存键、24 小时缓存、429/5xx 重试、`Retry-After` 和重定向后的域名再验证。
- `parser.py` 先递归搜索页面 JSON 状态中的 `bookId` 对象，再用可见书卡补字段；Scrapling 负责选择器，BeautifulSoup 兜底；还能识别“HTTP 200 但只有动态空壳”。
- `models.py` 以 `book_id → source_url → 书名|作者` 为去重键，并用详情页正常文字替换榜单页私有区字形。
- `pipeline.py` 固定执行榜单、详情、合并、搜索和指标统计；某榜解析为零时明确失败，不写成成功。
- `cli.py` 无论成功或失败都生成 `run-summary.json`，便于上层 Agent 判断结果。

它当前明确不下载小说正文，因此只能回答“哪些书在榜、题材是什么、字数和热度如何”，不能判断开篇节奏、章节钩子、人物弧和语言风格。早期单文件爬虫版本曾包含多线程和 Selenium 兜底；新系统应以模块化、低频、失败明确的适配器为基础，不采用随机 User-Agent 和自动浏览器降级思路。

### 8.2 新增 Reference Intelligence 应用

InkFlow 增加一个参考作品入口，但它不是第四个创作 Agent，而是工具与离线分析管线：

```text
榜单发现
→ 书目详情
→ 用户选择参考对象
→ 目录/文本导入
→ 清洗与章节切分
→ 分层分析
→ 特征卡
→ 参考 RAG
→ 写作前相似性防线
```

支持三种来源：

1. 用户本地导入 TXT、EPUB、MD、DOCX；这是最稳定的全文分析方式。
2. 普通浏览器无需登录、验证码或付费即可正常看到的公开网页正文、目录和书目信息。
3. 榜单、搜索页和详情元数据，用于发现样本、比较题材与热度，再由用户决定是否分析正文。

版权不作为创作流程里的高频阻断话题。系统默认用户会为自己的参考资料负责，重点做好来源记录、引用片段可见、特征抽取和相似度提示。技术边界只保留必要的一条：不实现登录凭据窃取、验证码破解、付费墙绕过或利用安全漏洞获取内容。正常可见的内容可以抓取、切章、评价和进入 Reference RAG；正文生成时主要使用结构特征与短证据，避免把整段参考原文误带进新章，这同时也更省 Token、更适合平台查重环境。

网站正文适配器采用 `SourceAdapter` 接口：

```python
class SourceAdapter(Protocol):
    def discover(self, query: DiscoverQuery) -> list[BookRef]: ...
    def fetch_book(self, ref: BookRef) -> BookMetadata: ...
    def fetch_catalog(self, ref: BookRef) -> list[ChapterRef]: ...
    def fetch_chapter(self, chapter: ChapterRef) -> ChapterPayload: ...
    def probe(self, ref: BookRef) -> SourceProbe: ...
```

`probe` 只判断页面是否公开可读、是静态正文还是动态空壳、需要哪种解析器以及当前失败原因。公开可见时允许正常抓取；遇到登录、验证码、付费或明确访问阻断时记录原因并交给用户选择其他导入方式，不尝试绕过。

抓取层继续采用低频 Session，但增加：

- ETag 与 Last-Modified 条件请求；
- 原始响应内容哈希与快照版本；
- 临时文件＋fsync＋rename 原子缓存；
- 每来源独立并发与请求预算；
- 任务队列、暂停、继续和失败隔离；
- 目录增量更新，只读取新增章节；
- 章节正文质量判断，识别空壳、错误页、登录页和重复页；
- HTML 清洗、正文选择器、标题和章节号标准化；
- 来源清单 `source_manifest.json`，记录 URL、时间、权限类型和解析器版本。

### 8.3 爆款分析不是“照着写”

全文分析器按层级执行，避免把几十万字一次塞给模型：

```text
章节文本
→ 场景级特征
→ 章节级摘要与功能
→ 每 10 章小弧统计
→ 每卷结构
→ 全书模式
→ 跨作品对比
```

评价维度包括：

- 前 1、3、10 章分别承诺了什么；
- 主角首次选择、首次胜利、首次失败出现在哪；
- 单章字数和场景数分布；
- 每章目标—阻力—选择—结果是否完整；
- 信息释放、误导和真相揭示的间隔；
- 战斗、对话、解释、心理和环境描写比例；
- 人物首次登场密度和关系变化速度；
- 爽点、代价、奖励和兑现周期；
- 章末钩子类型及连续重复率；
- 伏笔从种下到推进、兑现的平均跨度；
- 高频句式、段长、句长、感官通道和对白习惯；
- 题材标签与结构之间的真实关系。

系统内部产出结构化特征卡，用户需要时可导出 `reference_report.md`。生成时优先检索抽象特征和必要短证据，而不是反复加载整本参考书。最终章节与参考语料做连续 n-gram、长句近似和语义相似度提示；它是帮助创作者发现无意雷同的质量工具，不占据主要流程，也不因为一个普通题材套路相似就强制阻断。

## 9. 用户验收与文件工作流

### 9.1 用户只看到五类内容

模型混乱通常不是因为数据库表多，而是因为同一事实被投影到许多命名相似的文件里。用户工作区因此压缩为五个入口；除章节外，每类只保留一个当前视图：

```text
novel-project/
├─ BOOK.md                 # 本书契约、世界与主要角色的人类可读投影
├─ PLAN.md                 # 全书→卷→篇章→章节卡的唯一规划视图
├─ STATE.md                # 当前时间线、角色状态、未结线索和下一步
├─ chapters/
│  ├─ chapter_001.md       # 已接受章节
│  └─ chapter_002.draft.md # 待验收草稿
├─ reviews/
│  └─ chapter_002.review.md
└─ .inkflow/               # 引擎专用，普通模型不得自行遍历
   ├─ config.yaml
   ├─ inkflow.db
   ├─ index/
   ├─ cache/
   ├─ trash/
   └─ runs/<run_id>/
```

`BOOK.md`、`PLAN.md`、`STATE.md` 都是数据库的可编辑投影，不是第二套真相源。用户保存文件后，文件监视器生成结构化补丁，验证成功才写入数据库；若文件和数据库同时变化，则显示三方差异并要求选择。`.inkflow` 中的场景摘要、事实表、检索索引和历史版本不作为日常文件展示，也不允许宿主 Agent 自行猜测该读哪一个。

用户修改 `chapters/chapter_002.draft.md` 后只需在编辑器对话里说“重新审查并接受”。引擎先比较差异，再审查改动区及受影响事实，最后调用记忆 Agent 提交；不能因为用户手工修改就绕过记忆更新。

### 9.2 MCP 是统一操作面

对 Claude Code、VS Code、TRAE、Codex 暴露同一组带类型的工具，宿主只需把自然语言映射到它们：

```text
novel.project.create / open / status
novel.plan.book / volume / arc / chapter / validate / apply_patch
novel.context.build / explain
novel.chapter.write / review / patch / accept / reject
novel.memory.query / propose / commit / conflicts
novel.reference.discover / import / crawl / analyze / compare
novel.files.read / write / patch / move / delete / restore
novel.process.powershell
novel.trace.read
```

工具返回结构化结果、受影响文件、下一步建议和 trace ID。宿主 Agent 不直接调用模型供应商 API 写章，而是调用 Novel Engine；否则三个 Agent 的门禁、记忆事务和日志会被绕开。命令行入口只保留给开发调试、自动测试和故障恢复，不写进普通用户教程。

### 9.3 验收策略

验收策略有三种：

- `chapter_review`：默认，每章停下。
- `batch_review`：预授权写 N 章，但每章仍独立提交和可回滚，批次结束统一等待。
- `auto`：满足无阻断问题、记忆无冲突和成本预算后继续。

即使选择自动模式，卷末、重大反转、人物死亡、核心规则改变和大规模回溯修改仍默认暂停。

## 10. 工程实现、客户端适配与权限

### 10.1 一个代码库、五个内部包

代码可以模块化，但不应把内部包结构投射成几十份小说摘要。首版代码库压缩成五个业务包和一个适配层：

```text
inkflow/
├─ pyproject.toml
├─ src/inkflow/
│  ├─ engine/       # 状态机、检查点、计划门禁、恢复
│  ├─ agents/       # writer、reviewer、memory_keeper
│  ├─ knowledge/    # SQLite、RAG、Context Packet、索引
│  ├─ reference/    # 导入、爬虫适配、分层分析
│  ├─ providers/    # DeepSeek 与其他模型能力适配
│  └─ adapters/     # MCP、Claude Code、VS Code、TRAE、Codex、CLI
├─ templates/       # 工作区规则和客户端配置模板
└─ tests/
```

核心只有一个 Python 进程：MCP Server 与后台任务共用 Novel Engine；Qdrant 可以在第二阶段作为可选本地服务加入，SQLite 始终可单独运行。发布时提供单目录版 `InkFlow.exe` 初始化器和内置 Python 运行时。它负责安装/启动后台、生成宿主适配文件和打开示例项目，用户不需要在终端输入 `python inkflow.py`。CLI 仍存在，但只用于开发、CI 和宿主 Agent 在必要时调用。

适配器安装流程为：检测当前工作区和宿主 → 生成/合并规则文件 → 写入 MCP 配置 → 启动健康检查 → 在编辑器里返回“已连接、可用工具、当前项目状态”。生成器必须保留用户已有规则，使用带标记的 InkFlow 区块做幂等更新；不能整文件覆盖。因为 Claude Code、VS Code 和 TRAE 的配置格式可能随版本变化，具体模板带 `adapter_version`，由能力探测选择，而不是在小说引擎中写死路径。

### 10.2 高权限采用“可信工作区”而不是“只读沙盒”

按目标用户的使用方式，默认权限档设为 `trusted_workspace`。三 Agent 和工具可以在用户选定的小说项目根目录内读取、创建、写入、局部修改、移动和删除文件；可以运行爬虫、启动本地服务、执行 PowerShell、迁移数据库和重建索引。用户不需要对每个正常写章文件逐次确认。

```yaml
permission_profile: trusted_workspace
roots:
  - "${PROJECT_ROOT}"
filesystem: [read, create, write, patch, move, delete, restore]
network: [reference_crawl, model_api, dependency_metadata]
process:
  powershell: true
  cwd_must_be_in_roots: true
  background_services: [inkflow, qdrant]
database: [read, migrate, write, reindex, backup, restore]
delete_mode: recoverable
```

高权限的边界是“用户明确选择的根目录”，不是整块磁盘。默认删除先移动到 `.inkflow/trash/<timestamp>/` 并写 manifest，用户说“永久删除”时才硬删除；工作区之外的路径可由用户加入 `roots`。系统设置、其他项目、浏览器凭据、SSH Key、模型 Key 和权限策略本身仍不允许 Agent 擅自读取或修改。PowerShell 每次记录工作目录、命令摘要、退出码、输出摘要和受影响文件；这既满足自动化能力，也便于出错时回看。

### 10.3 可折叠过程记录：看得到依据，不依赖不可控的隐藏思维链

不同模型和宿主不一定会返回同一种原始推理内容，因此产品不能把“展示完整思维链”当成正确性前提。统一提供可审计的 Process Trace：阶段目标、采用的计划、关键理由、检索证据、工具/Skill 名称及版本、工具输入输出摘要、文件差异、Token/耗时、错误与重试、下一步。它足以让用户判断 Agent 为什么这样做。

若模型供应商明确返回 `reasoning_content`，并且用户开启“显示模型原始推理”，宿主可以把该字段放入临时折叠区；它不进入正史、不作为下章上下文，日志先脱敏，默认仅保留当前会话。跨宿主的最低公分母是 Markdown `<details>`：

```html
<details>
<summary>规划与决策</summary>
本章选择“错误胜利”，因为篇章中段需要主角主动决策并付出可追踪代价。
</details>

<details>
<summary>检索证据（8 条）</summary>
fact:warehouse_route；chapter:007:scene:2；thread:missing_seal……
</details>

<details>
<summary>工具与 Skill</summary>
novel.context.build → novel.chapter.write → novel.chapter.review
</details>
```

`.inkflow/runs/<run_id>/events.jsonl` 保存机器可读事件，`trace.md` 保存用户可读折叠视图。提供 `compact`、`standard`、`full` 三档：默认 `standard` 显示计划、证据、工具和差异；`full` 再显示完整工具参数、供应商返回的可用推理字段和各阶段用量。编辑器支持原生工具卡时优先使用原生 UI，不支持时打开 `trace.md`。

### 10.4 高关注度开源工具的取舍

以下 Star 数是 2026-08-30 的 GitHub 快照，只用于说明生态成熟度，不作为选型的唯一依据：

| 工具 | 快照 Star | 决策 | 在 InkFlow 中的角色 |
| --- | ---: | --- | --- |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 24.1K | 核心 | 一套工具协议适配 Claude Code、VS Code、TRAE、Codex，避免为每个编辑器重写业务 API |
| [Qdrant](https://github.com/qdrant/qdrant) | 34.3K | 第二阶段核心 | 派生向量/混合索引；SQLite 仍是唯一正史 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 79.9K | 标准可选 | 动态网页、正文抽取和 Markdown 清洗；接在 SourceAdapter 后，不接管状态机 |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | 77.1K | 核心爬虫组件 | 复用现有番茄工具已经验证的选择器与结构抽取路径 |
| [Playwright Python](https://github.com/microsoft/playwright-python) | 15.0K | 按需后备 | 只有静态请求拿到动态空壳时才启用，不作为所有来源的默认浏览器 |
| [PydanticAI](https://github.com/pydantic/pydantic-ai) | 19.6K | 可选薄层 | Provider 与结构化输出封装；若它不能保持 DeepSeek 特殊字段，就直接使用自有 Adapter |
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) | 11.2K | 开发期可选 | 离线追踪、RAG 评测与回归分析；终端用户仍看 InkFlow 自带 trace |
| [Mem0](https://github.com/mem0ai/mem0) | 64.3K | 不作为小说正史 | 可研究聊天偏好记忆，但人物状态和时间事实必须留在时态 SQLite |
| [LangGraph](https://github.com/langchain-ai/langgraph) / [CrewAI](https://github.com/crewAIInc/crewAI) | 40.7K / 57.8K | 首版不引入 | Novel Engine 已有确定性状态机，再叠图框架或群聊框架会产生第二套恢复和状态语义 |
| [browser-use](https://github.com/browser-use/browser-use) | 111.6K | 不作默认爬虫 | 通用浏览器 Agent 成本和不确定性较高；站点适配器＋Crawl4AI/Playwright 更易缓存、测试和限速 |
| [Unsloth](https://github.com/unslothai/unsloth) / [LLaMA-Factory](https://github.com/hiyouga/LlamaFactory) | 75.2K / 74.4K | 未来微调工具 | 等积累经用户同意的“草稿—修改—接受”数据后，再做 LoRA/SFT；不阻塞首版 |

结论是使用成熟组件解决协议、向量检索和网页解析，但保留自己的三 Agent 状态机、时态事实库和 Context Packet。高 Star 的通用 Agent 框架不等于更适合小说；首版尤其不能同时引入 LangGraph、CrewAI、Mem0 和 LlamaIndex，让四套记忆与编排概念争夺控制权。

API Key 优先保存到 Windows Credential Manager 或系统 keyring；开发环境可读 `.env`，但 `.env` 必须被 Git 忽略。日志统一脱敏，只显示 Key 后四位或哈希标识。先前在对话中出现过的 Key 应立即撤销，不能进入配置样例。

当前不做 Git 自动同步。代码中只预留 `SyncProvider` 接口，未来按照“个人宇宙”项目的策略接入：本地事实优先、用户显式触发、同步前生成变更摘要、冲突不自动覆盖。第一版不会自动创建仓库、提交或推送。

## 11. 缓存、成本与 Token 控制

长上下文并不意味着每章都重复付费。系统采用：

- 固定系统契约、工具 Schema、书籍契约和文风契约放在稳定前缀，尽量获得 DeepSeek 上下文缓存命中。
- 动态事实、RAG 证据、前章和本章任务放在后部。
- 不在 Agent 之间传递完整聊天历史，只传 typed artifact ID 和必要摘要。
- 审查从十几次调用压成一次综合调用，必要时再追加专项调用。
- 记忆 Agent 只分析通过版本和差异，不重复分析废弃草稿。
- 参考作品先离线分析成特征卡，写作时不重复读取全文。
- 本地 embedding 和 BM25 可离线运行，减少远程模型 Token。
- 每个模型运行记录 input、cache hit、cache miss、reasoning、output、耗时和估算成本。

正常章节预计调用：章节规划一次、正文一次、综合审查一次、记忆提取一次，共四次。需要修订时增加一次 PATCH 和一次复审。灵感模式只增加短方案调用，不生成多份完整章节。

## 12. 故障恢复与反崩坏机制

每个节点结束都写检查点，检查点包含：项目、章节、版本、步骤、输入摘要、输出摘要、模型运行 ID 和时间。写文件采用临时文件、刷盘、原子重命名；数据库写入使用事务。

恢复逻辑只读取事实：

- 有草稿无审查：从审查继续。
- 有审查且需修订：从 PATCH 继续。
- 用户已接受但记忆未提交：从原始 `memory_patch` 恢复，不能让模型重新提取一份不同补丁。
- 数据库已提交但向量未索引：只重跑索引。
- 章节显示完成但缺少 commit checkpoint：标记损坏并等待修复，不能猜测成功。
- 模型调用失败：指数退避并最多重试；鉴权和模型不存在直接停止。
- 审查调用失败：章节绝不放行。
- 同一节点五次未推动事实：熔断并生成 `recovery_report.md`。

用户可以回滚到任一已接受章节版本。回滚不是删除文件，而是新建一个修订分支，重新计算受影响章节后的事实和索引。已经发布的章节可标记为 `published_locked`，默认禁止自动改写。

## 13. 质量评测

系统上线前至少需要以下测试：

### 13.1 确定性测试

- 状态机全部路径和非法迁移；
- 事务失败回滚；
- 重复提交幂等；
- 任意节点崩溃后的恢复；
- 审查异常不放行；
- 用户许可只用于指定章节；
- Qdrant 删除后可从 SQLite 重建；
- 参考正文不能进入正史检索通道；
- `PLAN.md` 修改能生成补丁，不能直接造成章节范围重叠；
- 缺少卷、篇章或章节卡时，写作门禁必须停止；
- 同一事实无论内部有多少摘要，只能在 Context Packet 中得到一个明确有效状态。

### 13.2 RAG 测试

准备至少 100 个查询样例，包含人物位置、秘密、旧物品、跨卷伏笔、别名、相似人名和时间关系。衡量 Recall@K、证据覆盖率、错误事实注入率、检索 Token 占比和 reranker 改善幅度。每个结果必须能回到来源章节和正文区间。

### 13.3 长篇测试

- 20 章快速回归；
- 50 章多线并行；
- 100 章状态和伏笔压力测试；
- 人物误导与真实知识分离测试；
- 第 1 章埋线索、第 80 章兑现测试；
- 用户在第 30 章修改第 10 章后的影响分析。

### 13.4 文风与成熟度测试

单章评分不足以判断文风。至少连续五章统计句长、段长、对白比例、重复句、常用转折、章末钩子、感官分布和角色声音差异。模型 Judge 只做辅助，不能决定工作流是否提交。重要 Prompt 改动做 baseline/variant 各运行三次，再由人工读样决定。

## 14. 分阶段实现

### 阶段 A：可运行骨架

- MCP Server、通用 `AGENTS.md` 与 Claude Code/VS Code/TRAE/Codex 适配模板；
- 双击初始化器，普通用户无需终端；
- DeepSeek 官方 Provider Adapter；
- SQLite、五类用户可见文件和检查点；
- 三 Agent 状态机；
- 书→卷→篇章→章节四级规划和门禁；
- 单一 Context Packet 构建器；
- 逐章人工验收与可折叠 Process Trace；
- 暂不接 Qdrant，先用 SQL、最近章节和数据库派生摘要。

验收标准：用户在任一受支持编程 Agent 中打开项目并用自然语言完成创建、四级规划、写一章、查看过程、审查、手工修改、接受、提交记忆；关闭宿主后能继续下一章。整个流程不要求用户打开终端。

### 阶段 B：长篇 RAG

- Qdrant、BGE-M3、BM25 和 reranker；
- 时态事实、人物认知、伏笔与故事承诺；
- 256K/384K/512K 自适应上下文；
- Context Packet、continuity 视图和 retrieval trace；
- 50 章恢复与连续性测试。

### 阶段 C：参考作品应用

- 接入现有番茄榜单元数据工具；
- 本地 TXT/EPUB/MD 导入；
- 公开可见网页的 SourceAdapter、Scrapling 与 Crawl4AI/Playwright 后备；
- 参考作品分层分析和特征卡；
- 轻量相似度提示和跨作品比较。

### 阶段 D：大众化与打包

- 设置向导与四个预设；
- PyInstaller Windows 初始化器和后台服务打包；
- 各宿主适配器的版本探测、幂等升级和冲突合并；
- 自动诊断、日志脱敏和更新机制；
- Provider 能力探测和更多模型适配。

## 15. 当前默认决策

根据已确认需求，当前无需再阻塞开发的默认值为：

- 中文网文，单章 2000～4000 字；
- 面向普通用户与专业 AI 写手；
- DeepSeek 官方 API 作为测试基线，但架构不绑定模型；
- 全流程 Thinking，普通 high、复杂 max、简单聊天 low；
- 默认 256K，上浮 384K，卷末 512K；
- 默认逐章输出 MD 和审查报告，由用户确认；
- 主入口是 Claude Code、VS Code、TRAE、Codex 等编程 Agent 工作区；Python/MCP/EXE 是后台与安装层；
- 用户可见文件压缩为 `BOOK.md`、`PLAN.md`、`STATE.md`、章节与审查，模型每次只读一个 Context Packet；
- 规划固定为全书、卷、篇章、章节卡四级，当前篇章详细、远期滚动展开；
- `trusted_workspace` 默认开放项目内读写改删、爬虫和 PowerShell，删除默认可恢复；
- 过程以可折叠 trace 展示计划、依据、工具、Skill、差异和用量；供应商返回的推理字段可按需显示但不进入正史；
- 暂不导入既有小说；
- 暂不做 Git 同步，只预留接口；
- 参考作品支持本地文件与普通浏览器可公开看到的正文，重点做分层评价、特征学习和来源记录；
- 现有番茄工具先作为榜单发现与元数据输入，再通过可测试的 SourceAdapter 扩展正文识别。

仍需在编码阶段确定、但不影响总体架构的问题：不同版本 TRAE 的规则/MCP 模板；Qdrant 是随初始化器启动还是第二阶段安装；EPUB 解析是否首发；自动模式是否允许预授权多章；供应商原始推理字段的默认保留时长；参考作品相似度提示如何通过真实样本校准。这些都应通过适配模板、可替换配置和评测解决，而不是提前写死在核心流程里。
