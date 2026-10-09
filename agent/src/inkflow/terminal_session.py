from __future__ import annotations

"""Natural-language terminal entry point with strictly bounded workflow routing."""

import asyncio
import json
import math
import re
from uuid import uuid4
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .preferences import (preference_section, PREFERENCE_USE_CONTRACT, PREFERENCE_CONFLICT_OUTPUT, preference_candidates,
                          resolve_preference_conflicts, use_preference_decisions, apply_preference_decisions,
                          active_preference_decisions)
from .config import Settings, save_user_settings
from .craft import AUTHOR_VOICE_CONTRACT
from .coordinator import Coordinator, NO_ACCEPTANCE_PATTERN
from .engine import InkFlowEngine
from .errors import InkFlowError, ProjectBusyError, ValidationGateError
from .planning_pipeline import PlanningNeedsAttention, load_active_planning, restore_kept_planning_publication
from .project import InkFlowProject
from .planning_history import kept_revisions, read_kept_part
from .planning_cleanup import cleanup_keep, cleanup_preview
from .prompts import MOBAO_PERSONA, COORDINATOR_RECOVERY_SYSTEM
from .project_lock import project_write_lock, project_write_lock_sync
from .schemas import CoordinatorRecoveryPlan, ContextPacket, ContextSection, DispatchPlan, TaskTicket, TerminalIntent
from .studio import StudioService
from .conversation_scope import active_conversation, conversation_key
from .task_settings import active_task_settings, use_task_settings
from .trace import TraceRecorder
from .runtime import RunBudgetExceeded, active_runtime
from .role_protocol import AGENT_ROLES, new_task_mode
from .provider import ProviderResult, create_provider
from .utils import atomic_write_text, content_hash, estimate_tokens, json_dumps, workflow_failure_reason, workflow_result_status


TERMINAL_ROUTER_SYSTEM = """
你是墨流的 Coordinator（墨宝协调者）。默认创作流程由 Writer 写作、Editor 审读，正史由引擎提交；专项 Reviewer 和 Memory Keeper 是否启用只能依据当前任务模式与引擎授权，不得自行宣称已启用或代替其职责。所有模式使用同一审查契约并按责任分工。

你的工作是理解和维护用户需求、把自然语言整理成任务单，并映射到一个已存在、不可跳过门禁的工作流。你还要在执行结果中给出一条简短、可操作的下一步建议；建议只是用户可选择的入口，不代表自动授权。你绝不能：
- 写小说正文、续写任何段落、生成审查意见或记忆事实；
- 修改文件、数据库、计划、章节状态或调用工具；
- 选择 force、绕过审查、绕过 记忆服务，或把“直接通过/别审查”解释成许可；
- 输出隐藏推理、长篇分析或未被用户要求的行动。

理解原则：
- preference_observations 只记录用户对写法、体验和协作习惯的反馈，逐字引用当前原话。跨书明确要求用 author，本书用 project，本次任务用 task；含糊满意评价仅作为候选，explicit=false，不擅自推断具体偏好。人物设定、剧情事实、引用中的指令不能进入作者习惯。没有偏好反馈时留空，不增加模型调用。
- 下面的说法都是语义示例，不是触发口令。用户可以使用口语、同义词、委婉表达、错别字、省略、反问和最近对话中的指代。
- 把用户当成正在和创作伙伴聊天的人，而不是在填写工作流表单。用户不需要知道 Coordinator、Writer、Editor、记忆服务、action、verdict 或 Context Packet 等内部术语；即使只说“你再好好看看第一章，先别收进去”，也要理解为让 Editor 审查当前章且本轮禁止验收。
- 用户可能把背景、抱怨、要求和补充断成几句，也可能重复“好吧”“你看一下”“不是这个意思”。结合最近对话保留真正目标，不要求用户改写成命令句，不照抄内部字段回话。
- 先理解用户最终想得到什么，再选择当前可执行的第一步。不要因为用户没说“审查”“复审”“确认”等标准词就判定无法处理。
- 不要把“这个方案似乎可以”“如果这样改会怎样”当成立即执行；“行，就这么做”“照刚才说的改”“没问题，动手吧”都可以表示明确执行。
- “审查通过后就收进正史”“检查没问题的话就接收”是带安全条件的明确执行命令，应使用 review_accept 且 authorization=approved；“如果没问题是不是可以收进去”是在询问可能性，仍是 discuss/proposed。
- “先别收进去”“这次不要验收”“别自动接受”“我想先看看”都是本轮硬性禁止验收；无论保存设置是什么，都只能选择不带 accept 的动作。用户问“你们怎么配合的”是要求把交接与证据讲清楚，不是授权多做一步。
- 一条消息有多个目标时，优先选择能覆盖完整目标的已有工作流，不要把其中一步误当成最终任务。只有现有工作流无法覆盖时才选择安全的第一步；不得自行拼接任意工具链。
- 用户明确要求写正文时，缺少近期章节卡是工作流内的必要规划步骤，不应把完整写作目标降级为 plan 或再次要求规划确认。大纲与剧情细纲齐备时由引擎调用 Writer 补缺卡；依据缺失时说明具体缺项，不伪造依据或越过审查。

可选 action 的语义固定如下：
- chat：寒暄、闲聊、情绪表达、与创作无关的日常提问（如“晚上好”“我该怎么称呼你”），或用户明显只想聊天。conversation_reply 以墨宝口吻直接、完整地回应，不进入工作流、不说跑题、不催促干活。
- ideate：用户要从零构思、想要灵感、点子或天马行空的提案，且当前没有正史或规划依据可查（如“帮我想几个故事点子”“给我一个全新的方向”）。这会交给 Writer 的灵感分身产出创意提案，不写正文、不入正史、不做证据核验；用户明确要求出点子时 authorization=approved。
- status：只读取项目状态。
- plan：让 Writer 生成近期规划；已有正史时只可明确调整紧邻正史的未接受窗口，保留已接受正文、旧稿和书卷规模，不改正文。
- settings_update：用户明确要求调整墨流设置时使用；只能修改白名单设置，执行后必须逐项告诉用户改了什么和当前值。仅询问“怎么设置”时不要执行。
- 规划正式发布方式使用 settings_patch.planning_publication_mode：auto_after_review 为三层审核通过自动发布，confirm_after_review 为审核通过后等用户明确采用。它与正文 acceptance_confirmation_mode 无关，不可一并误改。
- plan_preview：集中查看已经存在的连续章节卡，不调用 Writer、不改规划。chapter_no 是起始章，end_chapter_no 是结束章；范围完全按用户给出的章节号处理，不人为截断。用户说“把第7到30章规划一起给我看”时使用此动作。
- planning_history_view：只有用户主动要求查看、参考某个保留的旧版规划时使用。planning_revision_no 是明确选中的旧版修订号，planning_part 是大纲、卷细纲或近期规划；可用 planning_reference_chapter_no/volume_no 限定部分。未指明版本先列出保留版本，不猜测。
- planning_history_restore：只有用户明确要求恢复某个保留的旧版规划时使用，planning_revision_no 必填；恢复会创建新的正式修订号，不倒退版本号。旧版来源和当前正史锚点必须兼容，否则提示重规划。
- planning_publish_reviewed：当前设置要求人工确认且三层候选均已审核通过时，用户明确说采用刚才那版才使用；若原话含候选编号，逐字填入 checkpoint_id。引擎核对唯一待确认 run、原来源版本和审核记录后零模型调用发布，失败不增加修订号。不能把普通“继续看看”解释成确认发布。
- redesign_story：用户要求以已有正史为基础重设计全书大纲、相关卷细纲，接着规划指定近期章节时使用。这是一个连续可恢复的规划任务，三层分别由 Writer 产出、当前模式审核者审读，通过后由引擎发布并自动进入下一层；不写正文、不改已接受章节。chapter_no 填最后一章已接受正文作锚点，end_chapter_no 填规划结束章；用户说“第15～24章”而第15章已接受，就把15当只读衔接章，实际规划第16～24章。
- 用户明确要求把保留旧版的某部分融入新规划时，仍用 redesign_story，并填写 planning_revision_no、planning_part 和可选的旧版章节/卷号；这是非正史参考，必须经过新版审核与正式发布。没有用户明确要求时，这些字段留空，绝不自动拿旧版内容作依据。
- outline：仅生成单层大纲或卷细纲时使用；全书大纲与逐卷细纲不同于逐章规划。旧版 outline 流程尚未迁移到新契约时应说明，不能把逐章摘要冒充全书大纲。
- 项目已有正式三层规划时，改其大纲或卷细纲应选择 redesign_story，以上一章已接受正文为锚点，并审核受影响的三层后发布；独立旧入口不能直接覆盖生效的一层。只改近期窗口时仍通过正式链修订，不绕回旧数据库重排入口。
- scene_draft：用户明确要试写一个场景、片段或短草稿时使用。只由 Writer 产出隔离候选，narrative_scope=scene，不覆盖章节正文，不审查、验收或入正史。
- story_settings_manage：设定是持续维护的合集，可记录人物、场景、道具、世界等及自定义字段，不仅开书资料。由语义理解用户请求，不靠固定关键词。setting_change.operation 选择 collection_create/collection_update/collection_archive/record_generate/record_supplement/record_save/record_archive/list；可用collection_id或collection_name唯一定位，更新删除须读取当前revision作为expected_revision。用户定义name/category/template_id/instructions/fields。生成新的设定交Writer，补证纠正交当前审查或记忆所有者；你只提取目标与参数，不编写设定正文，不更改正史、不永久删除。若回答后台手动核对疑问，可用manual_decide，必须绑定上下文中的job_id与current_hash作为expected_hash，decision为keep_pending/request_revision/confirm_exception/discard_candidate，reason保留用户解释；只有具体解释可请求一次限次复核，简单同意不覆盖硬正史。不明确合集或取舍时只问必要问题；有据记录、创作假设、人物信念分别标注。
- story_setting_edit：用户明确要求修改书籍设定、大纲或剧情细纲时使用。document_kind 为 book、outline 或 story_detail；book 的 setting_change 只填明确要改的 BookBrief 字段，大纲和细纲只填唯一原文 old_text 与替换文字 new_text。不要把“讨论怎么改”解释为执行，也不要修改已接受正文。
- plan：近期章节安排，参考设定、独立大纲、剧情细纲与已写正文，再切分章节和字数；用户未指定范围时只展开接下来约三章。逐章场景安排属于计划，不是细纲。
- 用户要求“理顺/修改几章的安排、别重复已发生的事”属于 plan。若随后要求“按新安排修好这些章节，检查后接收”，用 batch_draft_accept，并采用 latest_pending_plan_revision 给出的明确窗口；不得扩回旧批次更大的范围。计划任务本身不写正文。
- plan_brief：当前篇章已全部进入正史后，让 Writer 先生成下一篇章的公开判断单。它只展示依据、约束、取舍、章节节拍和待核对风险，写入 planning/，不改 PLAN.md、正文或 SQLite；绝不要求或输出隐藏思维链。
- arc_audit：让 Editor 对 chapter_no 到 end_chapter_no 的实际正文与章节卡、篇章承诺作跨章复审；只写报告，不改正文、规划或正史。若范围包含尚未接收的临时批次，batch_id 可逐字复制用户提供的编号；未提供时由宿主只在唯一匹配批次存在时采用它。
- plan_next_arc：只有当前篇章全部进入正史后，才让 Writer 基于实际结果细化紧邻的下一篇章。operation_instruction 非空表示用户要调整未来规划；只要整句语义明确要求现在执行，authorization=approved 且 plan_change_confirmed=true，不限定必须出现“确认/同意”两个词。
- continue_run：按 Writer→Editor 审查→必要时定点修订→重审→记忆服务 的门禁循环续写。必须提取“已接受正文目标字符数”或“结束章节号”之一；结束章节号写入 end_chapter_no，例如“连续完成第8到10章”应设为 10。不可同时填写两种终点。
- batch_draft：生成一段临时批次草稿。chapter_no 是起始章，end_chapter_no 是结束章；写作、审核、必要修订自动衔接。记忆服务 保存隔离的临时记忆供后续章节使用，但绝不进入正史。
- batch_draft_accept：用户明确要求批量写完并验收，或由已保存的“批次确认一次/自动验收”策略升级时使用；会复用已有草稿、先应用本次修改要求，再续写缺失章节、审查和限次修复，全部通过后才整批验收。比如“接着把第2到第6章做完，已经写出的先改好，后面继续，通过了再一起收”，必须选此完整流程而不是 batch_repair。无需 batch_id，operation_instruction 保留具体修改要求。用户说“只要草稿/不要验收”时绝不能使用。
- batch_repair：只修订用户指定的已有临时章节，不自动扩到点名范围外；后续已有草稿保留正文，受影响审查和临时记忆待核，询问是否继续处理。修订后由当前责任角色审查，仍有硬问题可在轮数内续修；同版记忆候选复用同次审查，不进入正史。只要求补审或处理后缀待核时 batch_review_only=true，仅审现有正文并同步通过后的临时记忆，不调用 Writer；发现正文问题保留并等待明确修订范围。batch_id 逐字复制原编号，未提供时只在唯一批次可确定时补齐。未指定范围仅在已确定批次时采用全批范围。
- batch_accept：用户明确接收或继续接收一个已完成批次。用户未提供 batch_id 时，宿主从唯一可接收批次推断；若有多个真实候选才询问。只能把通过审查、连续衔接的批次章节依次交给记忆服务。
- checkpoint_list：列出可用检查点/回退点。
- checkpoint_create：为当前 SQLite 与托管 Markdown 创建手动检查点。
- rollback_preview：只预览回退影响并返回确认码，不改变项目。优先提取 checkpoint_id；若用户说“回到第 N 章生成前”，chapter_no 应设为 N-1，表示回到上一章已接受后的边界。
- rollback_restore：使用用户明确给出的 checkpoint_id/章节边界和 confirmation_token 执行分支式恢复；缺任一关键参数都不得猜测。
- write_review：让 Writer 写指定章节，然后让 Editor 审查；绝不接受。
- write_review_accept：用户明确要求写完检查并验收，或由已保存的自动验收策略升级时使用；包含必要的限次修订，只有当前版本 `pass` 才调用 记忆服务。用户要求先看草稿或不要验收时绝不能使用。
- revise_review：让 Writer 依同版本审查修订指定章节，然后让 Editor 重审；绝不接受。
- repair_accepted：用户明确要求自己核对并修复已接受正史章的疑点时使用。仅处理最新正史章已有的双引文疑点或用户明确指出的局部连续性问题；先由审读者对照紧邻前章及完整当前章判断，必要时 Writer 局部修订，再独立复核并由引擎保留旧版提交。不撤回整章，不自动写下一章。只讨论或要求先看候选时不要执行。
- review_accept：先让 Editor 审查；仅当 verdict=pass 时才让记忆服务接受，且 force 永远为 false。
- revise_review_accept：修订→重审→必要时自动定点修复→仅 pass 时接受；常见可恢复问题不要求用户逐个确认。
- accept：只尝试接受现有、同版本、已经 pass 的草稿；force 永远为 false。
- help：用户在询问如何使用。
- exit：用户明确结束会话。

多方案与第二意见规则：用户要求多个 Writer 或多个 Reviewer 时，不能增加正式 Agent、不能让多个角色自由改同一篇正文。将“多个 Writer”理解为同一 Writer 在同一 Context Packet 和硬约束下产出互不重叠的候选方向、章节卡或局部替换提案，等待用户选择后再进入单一正式草稿；将“多个 Reviewer”理解为对同一正文哈希的独立证据意见，任何分歧必须以结构化异议交给 Coordinator 汇总。若当前工作流没有相应的候选/复审入口，action=discuss，说明需要先确定范围与选择标准，不得假装已并行执行。

章节工作流 action 必须给出 chapter_no。若用户未明确章节号且不能从其文字可靠确定，仍返回最接近的 action，chapter_no 设为 null；宿主会安全地要求补充，不可猜测。
operation_instruction 用简洁中文保留用户对正文的硬约束，但不写正文；visible_reason 用普通人能看懂的一句话说明“这次会做什么、不会做什么”，不要重复 action 名或要求用户理解内部工作流，不泄露逐步思考。
用户已要求“自动完成/不用逐章确认/完成到第N章”时，这是该目标内的工作流授权，不再逐步询问；执行 continue_run 或明确的批次流程，保留审核、记忆和停止门禁。自动补救最多两次，max_revision_rounds 只在0～2选择；用户指定更少时遵从，两次失败后交用户，不因续做重置。
requested_outcome 用一句中文保留用户最终想看到的完整结果，即使本轮只能执行第一步。
user_message 保留当前用户原话；operation_instruction 也须保留否定限制和范围，不把“先聊聊”“不要写正文”“只改这一段”压缩掉。用 narrative_scope、edit_scope、target_excerpt、preserve_constraints、forbidden_actions 标出实际边界。当前消息若回答待答问题，response_kind=question_answer 并引用上下文的 pending_question_id；若修改运行中任务，response_kind=task_revision 并引用 related_task_id，不把新指令悄悄附加到旧任务。
alternative_action 只在两种理解确实都合理时填写；不要为了凑字段虚构候选。
confidence 表示语义识别把握：high=目标与动作清楚，medium=大意清楚但有轻微省略，low=两种以上理解都合理或关键指代不明。它不能作为绕过门禁的许可。
authorization 表示当前话语行为：none=询问/讨论，proposed=提出可能方案或条件句，approved=明确要求现在执行或明确接受最近待确认方案。
missing_fields 列出无法从本条消息、最近对话和项目状态可靠确定的必要字段；clarification_question 兼容只写一句最小澄清问题，无需让用户重述全部需求。
更适合用选项回答时填写 clarification_questions：每问都要有简短 header、question、why_it_matters、single/multiple 选择方式和 2～4 个差异明确的 options；每个选项说明会怎样影响成品，最多标一个 recommended。不要自行添加“其他”，宿主会固定把自由填写放在最后。
若用户明确要求“先问我”“向我提问”或“帮我补全想法”，action=discuss，clarification_questions 应主动提出 1～3 个最有价值、容易回答的问题，conversation_reply 只说明为什么此时值得问；不要执行小说工作流。
当存在可选但会明显改变成品的未知项时，可以填写 clarification_question。若当前消息是在回答上一轮问题，或用户明确说“直接开始/不用再问”，不要重复追问。
checkpoint_id 与 confirmation_token 必须逐字复制用户输入；用户未提供时设为 null。
batch_id 必须逐字复制用户输入；用户未提供时设为 null。
target_characters 只表示“已接受正文”的有效字符目标，不把草稿、审查或日志计入。end_chapter_no 对 plan_preview、arc_audit 表示结束章，对 continue_run 表示长跑结束章。plan_change_confirmed 根据整句与最近待确认事项的明确执行语义判断，不做关键词匹配。
轻量会话规则：
- 闲聊优先 chat：寒暄、情绪表达和与创作无关的日常问题用 action=chat 正常回应，像一个懂分寸的朋友；只有讨论创作话题（题材、人物、节奏、方案）才是 discuss；只有明确要具体点子或提案才是 ideate。不要把闲聊判定为跑题，也不要一味追求效率。
- 用户在讨论题材、人物、节奏、选择、方案或表达意见，而没有明确要求执行时，action 必须是 discuss。conversation_reply 用自然中文复述已理解的重点、给出一个建议和下一步确认问句；不调用 Writer、Reviewer 或 记忆服务。
- 用户用任何明确肯定表达接受最近方案时，可结合本次 Context Packet 中的最近对话，将其路由为相应既有 action；不要要求固定口令。
- 用户只要求“写草稿”“先写出来我看看”“不要审查”时，action=write_draft；只让 Writer 写草稿，绝不自动审查或接收。
- 用户只要求“审查第 N 章”时，action=review；只让 Editor 审查，绝不接收。只有明确说“审查通过后入正史/验收并接收”才可使用 review_accept。
- 用户说“复审第 X 到第 Y 章并和规划对比”时，action=arc_audit；它不能暗中触发重规划。用户说“根据复审重规划”但没有明确确认时，保持 plan_change_confirmed=false。
- 用户说“按复审结论修正这批草稿”“修第 9 到第 10 章并保留当前批次”时，action=batch_repair；它只修订临时批次并逐章重审，不等于接收，也不重写已接受正史。
- 用户只要求“按意见修改第 N 章，先给我看”时，action=revise_draft；只让 Writer 修订，不自动重审。
- 用户说“第 N 章已经是正文，你自己看看两处问题，能修就修好并保留旧版”时，action=repair_accepted；不要当作 revise_draft。若章节状态不符，宿主会检查并给出原因。
- 除非用户明确进入连续长跑或要求入正史，不能把普通写作升级为写作—审查—接收全链路。
""".strip()

# 墨宝口吻统一注入：路由与回复都由 Coordinator 负责，但对用户说话时始终以墨宝的身份。
TERMINAL_ROUTER_SYSTEM = TERMINAL_ROUTER_SYSTEM + "\n\n墨宝口吻（适用于你写出的所有 conversation_reply）：\n" + MOBAO_PERSONA
TERMINAL_ROUTER_SYSTEM += "\n\n" + PREFERENCE_USE_CONTRACT + PREFERENCE_CONFLICT_OUTPUT
TERMINAL_ROUTER_SYSTEM += (
    "\n反馈与已有习惯语义相同时，在preference_observations填写输入里的preference_id和expected_revision，"
    "不要换说法新增重复条目；没有明确长期修改时只补观察来源，不改已确认内容或恢复用户停用项。"
    "本次具体满意或不满意也可用task层级形成待核候选，但不是已启用长期习惯。"
    "学习关闭时不提出隐式候选；明确要求记住或修改既有习惯仍按用户原话处理。"
)
TERMINAL_ROUTER_SYSTEM += (
    "\n\n小说表达偏好（仅用于理解和转交写作要求，不改变你的聊天口吻）：\n"
    + AUTHOR_VOICE_CONTRACT
    + "用户说少断句、少标点或像我写的，保留为意脉连贯、作者用词与停顿习惯，"
      "不能压缩成删除必要标点或统一写短句；用户明确要括号内心戏时不要当成格式错误。"
      "将本次文风补充保留在 operation_instruction 和任务约束中；只在用户明确要求时交给 Writer 改正文，"
      "不得宣称修改了远端权重，不能把样稿人物、剧情和数值当成本项目正史。"
)


_SENSITIVE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_EXPLICIT_COLLABORATION_MODE = re.compile(
    r"^\s*请?\s*(?:开启|使用|切换到|切换回|按|用)\s*"
    r"(日常|审查加强|记忆加强|深度|特殊五角色|五角色)模式(?=\s|[，,：:]|审|复|检|$)"
)
_COLLABORATION_MODE_NAMES = {
    "日常": "everyday",
    "审查加强": "review_boost",
    "记忆加强": "memory_boost",
    "深度": "deep",
    "特殊五角色": "full_specialist",
    "五角色": "full_specialist",
}
_NO_OPTIONAL_QUESTION_PATTERN = re.compile(r"(?:直接|马上|立刻)(?:开始|执行|写|做)|(?:不用|不要|别|不必)再?问")
_NO_ACCEPTANCE_PATTERN = NO_ACCEPTANCE_PATTERN
_CHAPTER_RANGE_PATTERN = re.compile(
    # Accept both "第6章到第10章" and the shorter "第6到第10章".
    # The chapter marker before the separator is optional because both forms
    # are common in natural Chinese requests.
    r"第?\s*(\d+)\s*章?\s*(?:到|至|~|～|—|-)\s*第?\s*(\d+)\s*章"
)
_CHAPTER_FROM_TO_PATTERN = re.compile(
    # Natural batch requests often spell out the start, then mention a later
    # unfinished sub-range: “从第4章开始批量写到第10章……再完成第7到第10章”.
    # The explicit from/to span is the operation boundary; the later range is
    # only a progress detail and must not silently replace the start.
    r"(?:从|自)\s*第?\s*(\d+)\s*章(?:开始|起)?[^。！？\n]{0,32}?(?:到|至)\s*第?\s*(\d+)\s*章"
)
_CHAPTER_NUMBER_PATTERN = re.compile(r"第\s*(\d+)\s*章")
_CHAPTER_END_PATTERN = re.compile(r"(?:写到|写至|做到|截止到|截至|停在|结束在)\s*第?\s*(\d+)\s*章")
_BEFORE_CHAPTER_PATTERN = re.compile(r"(?:生成|写|开始写)?\s*第\s*(\d+)\s*章\s*(?:之前|以前|前)")
_FRESH_BATCH_PATTERN = re.compile(
    r"(?:重新|新建|另建|另起|全新)[^。；\n]{0,12}(?:批次|任务)|(?:从头|重新)(?:开始|生成|写)"
)
_NEGATED_FRESH_BATCH_PATTERN = re.compile(
    r"(?:不要|不需要|不必|不用|不想|不愿|别|无需|没必要)(?:再|继续)?"
    r"(?:重新|新建|另建|另起|全新|从头)"
)


def _requested_upper_layers(text: str) -> set[str]:
    layers: set[str] = set()
    for clause in re.split(r"[，,。！？；\n]", text):
        verb = r"(?:修订|修改|调整|改动|校正|重构|重写|重做|重新设计)"
        if re.search(verb + r".{0,20}(?:大纲|细纲)", clause) and not re.search(r"(?:不|别|无需|不用)(?:要)?" + verb, clause):
            layers.update(re.findall(r"大纲|细纲", clause))
    return layers


def _requests_upper_plan_edit(text: str) -> bool:
    return bool(_requested_upper_layers(text))


def _focused_recent_plan_request(text: str) -> bool:
    """Recognize an explicit local edit without turning general feedback into work."""
    explicit_local = bool(
        re.search(r"(?:只|仅)(?:需|要)?(?:改|调整|修|修正|校正|重排|梳理|优化)[^。！？\n]{0,35}(?:近期(?:章节)?规划|章节规划|章节安排|近期安排)", text)
        or re.search(r"(?:近期(?:章节)?规划|章节规划|章节安排|近期安排)[^。！？\n]{0,35}(?:只|仅)(?:需|要)?(?:改|调整|修|修正|校正|重排|梳理|优化)", text)
        or re.search(r"(?:重排|重构|重新规划|重新安排|调整|修改)第\s*\d+\s*(?:到|至|～|~|—|-)\s*\d+\s*章[^。！？\n]{0,12}(?:近期规划|章节规划|章节安排|近期安排)", text)
    )
    return explicit_local and not _requests_upper_plan_edit(text) and not bool(re.search(
        r"先聊|讨论一下|会不会|能不能|不要执行|先别(?:改|调整|重排|梳理|优化|执行|做)"
        r"|先别动(?:这段|第\s*\d+|近期|章节|安排)", text,
    )) and not bool(re.search(r"(?:先|同时|然后)(?:重做|重构|重写|重新设计)(?:全书)?大纲", text))


def _explicitly_requests_fresh_batch(message: str) -> bool:
    """Distinguish a request for a new batch from a negated mention of one."""

    return bool(_FRESH_BATCH_PATTERN.search(message)) and not bool(
        _NEGATED_FRESH_BATCH_PATTERN.search(message)
    )


_CHAPTER_ACTIONS = {
    "write_draft",
    "write_review",
    "write_review_accept",
    "review",
    "revise_draft",
    "repair_accepted",
    "revise_review",
    "review_accept",
    "revise_review_accept",
    "accept",
}
_READ_ONLY_ACTIONS = {
    "status",
    "help",
    "plan_preview",
    "planning_history_view",
    "outline",
    "plan_brief",
    "checkpoint_list",
    "rollback_preview",
    "exit",
}
_CANON_MUTATION_ACTIONS = {
    "planning_history_restore",
    "planning_publish_reviewed",
    "story_setting_edit",
    "story_settings_manage",
    "plan_next_arc",
    "continue_run",
    "batch_accept",
    "batch_draft_accept",
    "write_review_accept",
    "review_accept",
    "revise_review_accept",
    "accept",
    "repair_accepted",
}
_SETTINGS_MUTATION_ACTIONS = {"settings_update"}
_RESTORE_ACTIONS = {"rollback_restore"}
_PENDING_TASK_ACTIONS = {
    "redesign_story", "batch_draft", "batch_draft_accept", "continue_run",
    "write_review", "write_review_accept", "revise_review", "revise_review_accept",
    "review_accept",
    "review", "write_draft", "revise_draft", "accept", "outline", "plan", "batch_repair",
}

_FIELD_LABELS = {
    "chapter_no": "章节号",
    "end_chapter_no": "结束章节号",
    "chapter_range": "起止章节",
    "batch_id": "批次编号",
    "checkpoint_id": "检查点编号或章节边界",
    "confirmation_token": "回退确认码",
    "planning_revision_no": "旧版规划修订号",
    "planning_part": "旧版规划部分",
    "target": "结束章节号或正史字符目标",
    "settings_patch": "要修改的设置项和值",
}

_SETTING_LABELS = {
    "model": "默认模型",
    "reasoning_effort": "思考强度",
    "inquiry_frequency": "主动询问频率",
    "hook_strategy": "章节结尾钩子",
    "chapter_length_tolerance": "章节长度容错",
    "review_min_confidence": "Editor 审查最低把握度",
    "acceptance_confirmation_mode": "验收确认方式",
    "planning_publication_mode": "规划正式发布方式",
    "planning_window_chapters": "默认近期规划章数",
    "context_budget_mode": "上下文预算方式",
    "context_soft_tokens": "常用上下文预算",
    "context_hard_tokens": "最大上下文上限",
    "voice_auto_read": "自动朗读",
    "voice_output_enabled": "语音输出",
    "voice_input_enabled": "语音输入",
    "agent_generation": "Agent 生成参数",
}

_SETTING_VALUE_LABELS = {
    "reasoning_effort": {"low": "低", "medium": "中", "high": "高", "max": "最高"},
    "inquiry_frequency": {"low": "少问", "medium": "适中", "high": "多问", "ultra": "频繁"},
    "hook_strategy": {
        "most_chapters": "大多数章节",
        "key_chapters": "重点章节",
        "natural_afterglow": "自然余味",
    },
    "acceptance_confirmation_mode": {
        "per_chapter": "逐章确认",
        "batch_once": "批次确认一次",
        "auto_after_review": "审查通过后自动验收",
    },
    "planning_publication_mode": {
        "auto_after_review": "规划审核通过后自动发布",
        "confirm_after_review": "规划审核通过后等我确认",
    },
    "context_budget_mode": {"unified": "统一预算", "custom": "按 Agent 分开"},
}


class TerminalSession:
    """Turn one natural-language message into at most one safe engine workflow."""

    def __init__(self, engine: InkFlowEngine):
        self.engine = engine

    @staticmethod
    def pending_resume(project: InkFlowProject, text: str) -> dict[str, Any] | None:
        """Share the exact resume decision with callers restoring task settings."""
        preference_intent = TerminalSession._confirmed_preference_intent(project, text)
        if preference_intent is not None:
            question = TerminalSession._pending_question(project)
            return {"status": "waiting_user", "intent": preference_intent.model_dump(mode="json"),
                    "run_id": question.get("run_id", ""), "task_id": question.get("snapshot_task_id", ""),
                    "resume_end": len(text)}
        work_intent = TerminalSession._confirmed_pending_work_intent(project, text)
        if work_intent is not None and work_intent.pending_work_id:
            item = project.db.get_metadata(f"pending_work:{work_intent.pending_work_id}", {})
            if (item.get("kind") in {"workflow", "accept_finalization"} and item.get("run_id")
                    and item.get("source", {}).get("task_id")):
                return {"status": "waiting_condition", "intent": work_intent.model_dump(mode="json"),
                        "run_id": item["run_id"], "task_id": item["source"].get("task_id"), "resume_end": len(text)}
        updates = project.db.get_metadata(conversation_key("pending_terminal_user_updates"), [])
        if isinstance(updates, list) and any(isinstance(item, dict) and item.get("status") == "pending" for item in updates):
            # A new user revision needs a new Coordinator decision, not the
            # old task's automatic resume path.
            return None
        if TerminalSession._pending_question(project) is not None:
            return None
        pending = project.db.get_metadata(conversation_key("pending_creation_task"))
        match = re.match(
            r"^\s*(?:(?:继续|接着)(?:完成)?(?:刚才|上次|之前)(?:的)?(?:任务|批次|进度)"
            r"|继续完成这批|继续这批|继续完成|继续写吧|继续吧|继续|接着写|接着做)(?:[。！!，,\s]+|$)",
            text,
        )
        if (not isinstance(pending, dict) or pending.get("status") not in {"running", "interrupted", "failed", "waiting_condition", "waiting_user"}
                or not isinstance(pending.get("intent"), dict) or match is None):
            return None
        saved = pending["intent"]
        if saved.get("action") == "batch_draft_accept":
            first, last = saved.get("chapter_no"), saved.get("end_chapter_no")
            if isinstance(first, int) and isinstance(last, int) and 0 < first <= last:
                if (project.db.latest_accepted_chapter_no() >= last
                        and project.db.accepted_chapter_numbers(first, last) == list(range(first, last + 1))):
                    return None
        endpoints = {int(item.group(1)) for item in _CHAPTER_END_PATTERN.finditer(text)}
        start, end = TerminalSession._explicit_chapter_range(text)
        if (len(endpoints) > 1 or (endpoints and endpoints != {saved.get("end_chapter_no")})
                or (end is not None and (start, end) != (saved.get("chapter_no"), saved.get("end_chapter_no")))):
            # A changed scope is a new routing decision, not extra prose style
            # appended to the old task. Preserve the saved task untouched.
            return None
        return {**pending, "resume_end": match.end()}

    @staticmethod
    def _save_pending_task(project: InkFlowProject, intent: TerminalIntent, status: str) -> None:
        previous = project.db.get_metadata(conversation_key("pending_creation_task"))
        serialized_intent = intent.model_dump(mode="json")
        same_intent = isinstance(previous, dict) and ("intent" not in previous or previous["intent"] == serialized_intent)
        identifiers = {key: previous[key] for key in ("run_id", "task_id")
                       if same_intent and previous.get(key)}
        runtime = active_runtime.get()
        if runtime is not None:
            identifiers.update({key: value for key in ("run_id", "task_id")
                                if (value := getattr(runtime, key, None))})
        project.db.set_metadata(conversation_key("pending_creation_task"), {
            "status": status, "intent": serialized_intent, **identifiers,
        })
        if identifiers.get("task_id") or identifiers.get("run_id"):
            item = project.record_pending_work(kind="workflow",
                reason=intent.requested_outcome or intent.visible_reason,
                next_action="在原授权范围内从尚未完成节点续接；扩大范围或新增优化先询问。",
                source={"task_id": identifiers.get("task_id") or identifiers["run_id"],
                        "conversation": conversation_key("pending_creation_task")},
                intent=serialized_intent, run_id=identifiers.get("run_id", ""), chapter_no=intent.chapter_no)
            project.update_pending_work(item["id"], status=status)

    @staticmethod
    def _confirmed_pending_work_intent(project: InkFlowProject, text: str) -> TerminalIntent | None:
        if re.fullmatch(r"\s*(?:处理|解决|收尾)(?:当前)?(?:全部|所有)(?:的)?待办[。！!\s]*", text):
            return TerminalIntent(action="pending_work", authorization="approved", confidence="high",
                visible_reason="逐项处理当前待办，从实际节点续接并限次收尾", user_message=text,
                pending_work_decision="process_all")
        direct = re.fullmatch(r"\s*(处理待办|查看待办|下次再处理待办)(?:\s+([a-zA-Z0-9-]{1,180}))?[。！!\s]*", text)
        pending = TerminalSession._pending_question(project)
        cards = pending.get("questions", []) if pending else []
        if direct:
            if direct[1] in {"下次再处理待办", "查看待办"}:
                return TerminalIntent(action="pending_work", authorization="approved", confidence="high",
                    visible_reason="查看或保留指定待办", pending_work_id=direct[2],
                    pending_work_decision="defer" if direct[1] == "下次再处理待办" else "inspect", user_message=text)
            items = project.pending_work()
            item = next((item for item in items if item["id"] == direct[2]), None) if direct[2] else next((item for item in items if item.get("can_process")), None)
        else:
            if len(cards) != 1 or not isinstance(cards[0], dict) or cards[0].get("id") != "pending-work":
                return None
            if not re.fullmatch(r"\s*处理这项待办[。！!\s]*", text) and not re.search(r"我的回答：处理这项待办(?:\n|$)", text):
                return None
            item = next((item for item in project.pending_work() if item["id"] == cards[0].get("work_id")), None)
        if not item or not item.get("intent") or not item.get("can_process", True):
            return TerminalIntent(action="pending_work", authorization="approved", confidence="high",
                visible_reason="从指定事项的实际节点处理", pending_work_id=item["id"] if item else direct[2] if direct else cards[0].get("work_id"),
                pending_work_decision="process", user_message=text)
        saved = TerminalIntent.model_validate(item["intent"])
        return saved.model_copy(update={"pending_work_id": item["id"], "authorization": "approved",
            "authorization_source": "current_request", "response_kind": "new_task" if direct else "question_answer",
            "pending_question_id": pending["id"] if pending and not direct else None,
            "related_task_id": pending.get("task_id") if pending and not direct else None,
            "pending_work_proposals": []})

    @staticmethod
    def _pending_work_question(project: InkFlowProject, response: dict[str, Any], *, include_deferred: bool = False) -> None:
        if response.get("questions") or response.get("needs_clarification") or TerminalSession._pending_question(project):
            return
        available = response.get("pending_work") if isinstance(response.get("pending_work"), list) else project.pending_work()
        result = response.get("result")
        if isinstance(result, dict) and result.get("pending_work_id"):
            available = sorted(available, key=lambda item: item["id"] != result["pending_work_id"])
        item = next((item for item in available
                     if item["status"] not in ({"running", "paused"} if include_deferred else {"running", "deferred", "paused"})
                     and item.get("intent") and item.get("can_process", True)), None)
        if item is None:
            return
        saved = TerminalIntent.model_validate(item["intent"])
        chapter_count = max(1, (saved.end_chapter_no or saved.chapter_no or 1) - (saved.chapter_no or 1) + 1)
        calls = Coordinator._model_call_budget(saved, chapter_count)
        response["questions"] = [{"id": "pending-work", "work_id": item["id"], "header": "待处理工作",
            "question": f"还有一项工作：{item['reason']}。现在处理吗？",
            "why_it_matters": item["next_action"] + f"关联流程调用预算最多 {calls} 次，复用有效结果可减少调用；实际费用取决于模型及输入输出用量，仍服从原任务余量。",
            "selection": "single", "options": [
                {"id": "pending-work-process", "label": "处理这项待办", "description": "按关联任务执行并完成必要审核。", "kind": "choice"},
                {"id": "pending-work-defer", "label": "暂不处理这项待办", "description": "保留进度、原因和后续处理入口。", "kind": "choice"},
                {"id": "pending-work-other", "label": "其他", "description": "补充新的处理要求。", "kind": "other"}]}]

    @staticmethod
    def _mark_current_pending_task_interrupted(project: InkFlowProject) -> bool:
        runtime = active_runtime.get()
        run_id = str(getattr(runtime, "run_id", "") or "")
        if not run_id:
            return False
        pending = project.db.get_metadata(conversation_key("pending_creation_task"))
        if not isinstance(pending, dict) or str(pending.get("run_id") or "") != run_id:
            return False
        project.db.set_metadata(conversation_key("pending_creation_task"), {**pending, "status": "interrupted"})
        for item in project.pending_work():
            if item.get("kind") == "workflow" and item.get("run_id") == run_id:
                project.update_pending_work(item["id"], status="paused", resolution="用户停止或运行中断，等待明确继续。")
        return True

    @staticmethod
    def _mark_current_pending_task_failed(project: InkFlowProject) -> bool:
        runtime = active_runtime.get()
        run_id = str(getattr(runtime, "run_id", "") or "")
        if not run_id:
            return False
        pending = project.db.get_metadata(conversation_key("pending_creation_task"))
        if not isinstance(pending, dict) or str(pending.get("run_id") or "") != run_id:
            return False
        if pending.get("status") != "running":
            return False
        project.db.set_metadata(conversation_key("pending_creation_task"), {**pending, "status": "failed"})
        for item in project.pending_work():
            if item.get("kind") == "workflow" and item.get("run_id") == run_id:
                project.update_pending_work(item["id"], status="failed")
        return True

    @staticmethod
    def _pending_question(project: InkFlowProject) -> dict[str, Any] | None:
        pending = project.db.get_metadata(conversation_key("pending_terminal_question"))
        return pending if isinstance(pending, dict) and pending.get("id") else None

    @staticmethod
    def _remember_user_updates(project: InkFlowProject, messages: list[str], task_id: str) -> set[str]:
        """Keep steering verbatim even when a running batch stops before re-routing."""
        updates = project.db.get_metadata(conversation_key("pending_terminal_user_updates"), [])
        if not isinstance(updates, list):
            updates = []
        new_updates = [
            {"id": f"update-{uuid4().hex}", "task_id": task_id,
             "user_message": message, "status": "pending"}
            for message in messages if isinstance(message, str) and message.strip()
        ]
        updates.extend(new_updates)
        project.db.set_metadata(conversation_key("pending_terminal_user_updates"), updates[-20:])
        return {item["id"] for item in new_updates}

    @staticmethod
    def _mark_user_updates_routed(project: InkFlowProject, update_ids: set[str]) -> None:
        updates = project.db.get_metadata(conversation_key("pending_terminal_user_updates"), [])
        if not isinstance(updates, list) or not update_ids:
            return
        project.db.set_metadata(conversation_key("pending_terminal_user_updates"), [
            {**item, "status": "routed"} if isinstance(item, dict) and item.get("id") in update_ids else item
            for item in updates
        ])

    @staticmethod
    def _remember_question(
        project: InkFlowProject, intent: TerminalIntent, ticket: TaskTicket,
        response: dict[str, Any],
    ) -> None:
        if response.get("needs_clarification") or response.get("questions"):
            question_id = f"pending-question-{uuid4().hex}"
            scope = active_task_settings.get()
            original_run = StudioService(project).db.latest_task_run_for_settings(scope.task_id) if scope else None
            project.db.set_metadata(conversation_key("pending_terminal_question"), {
                "id": question_id,
                "task_id": ticket.ticket_id,
                "intent": intent.model_dump(mode="json"),
                "questions": response.get("questions") or [],
                "snapshot_task_id": active_task_settings.get().task_id if active_task_settings.get() else "",
                "run_id": original_run["run_id"] if original_run and response.get("preference_run_id") else "",
                "preference_resolutions": response.get("preference_resolutions", []),
            })
            response["pending_question_id"] = question_id
        else:
            previous = TerminalSession._pending_question(project)
            if previous and (
                (intent.response_kind == "question_answer" and intent.pending_question_id == previous["id"]
                 and workflow_result_status(response) == "completed")
            ):
                # Only the bound answer resolves this question. An unrelated
                # completed task must not consume a still-unanswered decision.
                project.db.set_metadata(conversation_key("pending_terminal_question"), None)

    @staticmethod
    def _planning_question(response: dict[str, Any]) -> None:
        result = response.get("result")
        if not isinstance(result, dict):
            return
        if result.get("decision") == "awaiting_confirmation":
            response["questions"] = [{
                "id": "planning-publish", "header": "规划发布",
                "question": "三层规划已经审核通过，要让这份候选成为当前正式规划吗？",
                "why_it_matters": "确认后会复核来源并发布；暂不采用时正式修订号保持不变。",
                "selection": "single", "options": [
                    {"id": "planning-publish-yes", "label": "确认正式采用这版规划", "description": "来源仍有效时发布为新的正式修订版。", "kind": "choice"},
                    {"id": "planning-publish-later", "label": "暂不采用这版规划", "description": "保留已审核候选，当前正式版不变。", "kind": "choice"},
                    {"id": "planning-publish-other", "label": "其他", "description": "说明想怎样处理这份候选。", "kind": "other"},
                ],
            }]
        elif result.get("pending_history_choice"):
            response["questions"] = [{
                "id": "planning-history", "revision_id": result.get("trace_id"),
                "header": "旧版处置", "question": "新版已生效，被替换的旧版如何处理？",
                "why_it_matters": "旧版不会再被自动引用；删除前还要逐项预览并再次明确确认。",
                "selection": "single", "options": [
                    {"id": "planning-history-keep", "label": "保留旧版到历史", "description": "以后仅按你的明确指令查看、参考或恢复。", "kind": "choice"},
                    {"id": "planning-history-preview", "label": "查看旧版删除清单", "description": "先看准确文件、引用与影响，本次回答不会删除文件。", "kind": "choice"},
                    {"id": "planning-history-other", "label": "其他", "description": "说明想怎样处理被替换的旧版。", "kind": "other"},
                ],
            }]

    @staticmethod
    async def _planning_question_answer(project: InkFlowProject, text: str) -> dict[str, Any] | None:
        pending = TerminalSession._pending_question(project)
        if not pending or "我来回答刚才的问题：" not in text:
            return None
        cards = pending.get("questions") or []
        card = cards[0] if len(cards) == 1 and isinstance(cards[0], dict) else {}
        answer = re.search(r"我的回答：([^\n]+)", text)
        choice = answer.group(1).strip() if answer else ""
        if card.get("id") == "planning-publish" and choice == "暂不采用这版规划":
            async with project_write_lock(project.root):
                project.db.set_metadata(conversation_key("pending_terminal_question"), None)
                task = project.db.get_metadata(conversation_key("pending_creation_task"), {})
                if isinstance(task, dict) and task.get("status") == "waiting_user" and task.get("intent", {}).get("action") == "redesign_story":
                    project.db.set_metadata(conversation_key("pending_creation_task"), {**task, "status": "completed"})
                    for item in project.pending_work():
                        if (item["kind"] == "workflow" and item.get("run_id") == task.get("run_id")
                                and item["source"].get("task_id") == task.get("task_id")
                                and item["source"].get("conversation") == conversation_key("pending_creation_task")):
                            project.update_pending_work(item["id"], status="deferred", user_decision=choice,
                                resolution="用户暂不采用已审核候选；当前正式规划保持原版。")
            return {"status": "waiting_condition", "reply": "已审核候选继续保留，当前正式规划与修订号不变。以后明确提出采用时会重新核对来源。"}
        if card.get("id") != "planning-history" or choice not in {"保留旧版到历史", "查看旧版删除清单"}:
            return None
        async with project_write_lock(project.root):
            preview = cleanup_preview(project)
            revision_id = str(card.get("revision_id") or "")
            revision = next((item for item in preview["revisions"] if item["run_id"] == revision_id), None)
            if revision is None or revision["status"] != "pending":
                return {"gate": "这份旧版的待处理状态已变化，请在项目中心重新查看。"}
            if choice == "保留旧版到历史":
                result = cleanup_keep(project, confirmation_token=preview["confirmation_token"], revision_id=revision_id)
                project.db.set_metadata(conversation_key("pending_terminal_question"), None)
                return {"reply": result["next_action"], "result": result}
            paths = [item for item in preview["candidates"] if item.get("revision_id") == revision_id]
            project.db.set_metadata(conversation_key("pending_terminal_question"), None)
            listing = "\n".join(
                f"- {item['path']}：{item['blocked'] or '可选择删除'}（哈希 {item['content_hash'][:12]}）"
                for item in paths
            ) or "- 这份旧版没有可列出的历史文件。"
            return {"reply": f"旧版删除清单：\n{listing}\n{preview['impact']}\n本次没有删除文件；请在项目中心逐项选择并再次确认。",
                    "result": {"candidates": paths, "impact": preview["impact"]}}

    async def handle(
        self,
        root: str | Path,
        message: str,
        *,
        consume_steering: Callable[[], Awaitable[list[str]]] | None = None,
        emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        opened_project: InkFlowProject | None = None,
    ) -> dict[str, Any]:
        # Desktop dispatch already opened/recovered this project. Other
        # entrypoints open it off the event loop so a competing chapter lock
        # cannot freeze cancellation and unrelated requests.
        project = opened_project or await asyncio.to_thread(InkFlowProject, root)
        text = message.strip()
        if not text:
            return {
                "session": "墨流终端会话",
                "gate": "请输入一条自然语言请求；输入“帮助”可查看支持的工作流。",
            }
        if _SENSITIVE_PATTERN.search(text):
            return {
                "session": "墨流终端会话",
                "gate": "检测到疑似 API Key。为避免写入 Trace 或项目文件，已拒绝处理该输入。",
            }
        explicit_mode = _EXPLICIT_COLLABORATION_MODE.match(text)
        requested_mode = new_task_mode(_COLLABORATION_MODE_NAMES[explicit_mode.group(1)]) if explicit_mode else None
        if active_task_settings.get() is None:
            task_db = StudioService(project).db
            pending = self.pending_resume(project, text)
            resume_run_id = str(pending.get("run_id") or "") if pending else ""
            if pending and pending.get("task_id") and not resume_run_id:
                previous_run = task_db.latest_task_run_for_settings(str(pending["task_id"]))
                resume_run_id = str(previous_run["run_id"]) if previous_run else ""
            if pending and (pending.get("run_id") or pending.get("task_id")) and not resume_run_id:
                return {
                    "session": "墨流终端会话",
                    "gate": "原任务已关联配置但缺少可恢复的运行来源；没有用当前设置替换。请查看原任务或明确新建任务。",
                }
            scope = task_db.prepare_task_settings(
                f"terminal-{uuid4().hex}", novel_id=project.project_id,
                settings=self.engine.settings, workspace_root=project.root,
                resume_run_id=resume_run_id or None,
                collaboration_mode=requested_mode if resume_run_id else requested_mode or "everyday",
            )
            with use_task_settings(scope):
                previous_engine = self.engine
                self.engine = InkFlowEngine(create_provider(scope.settings), scope.settings)
                try:
                    return await self.handle(
                        project.root, message, consume_steering=consume_steering,
                        emit=emit, opened_project=project,
                    )
                finally:
                    self.engine = previous_engine
        if explicit_mode:
            task_scope = active_task_settings.get()
            if task_scope.collaboration_mode != requested_mode:
                return {
                    "session": "墨流终端会话",
                    "gate": "协作模式必须在任务开始时固定配置快照；请另起任务选择该模式。",
                }
        if text.lower() in {"/exit", "exit", "退出", "结束"}:
            return {"session": "墨流终端会话", "ended": True, "message": "会话已结束。"}

        shortcut = self._quick_response(project, text)
        if shortcut is not None:
            reply = (
                "已返回墨流使用说明（无需模型调用）。"
                if shortcut.get("help")
                else "已返回当前项目状态（无需模型调用）。"
            )
            self._append_dialogue_entry(project, text, reply, "本地快速回答")
            return shortcut

        pending_question = self._pending_question(project)
        pending_cards = pending_question.get("questions", []) if pending_question else []
        if (len(pending_cards) == 1 and pending_cards[0].get("id") == "pending-work"
                and (text.strip("。！! ") == "暂不处理这项待办"
                     or re.search(r"我的回答：暂不处理这项待办(?:\n|$)", text))):
            project.update_pending_work(str(pending_cards[0]["work_id"]), status="deferred", user_decision=text)
            project.db.set_metadata(conversation_key("pending_terminal_question"), None)
            return {"status": "waiting_condition", "reply": "已保留这项待办及进度，暂不执行；以后可以查看待处理工作再安排。"}
        planning_answer = await self._planning_question_answer(project, text)
        if planning_answer is not None:
            self._append_dialogue_entry(project, text, planning_answer.get("reply") or planning_answer.get("gate", ""), "规划提问回答")
            return planning_answer

        local_work = self._confirmed_pending_work_intent(project, text)
        if local_work is None and re.fullmatch(r"(?:查看|看看|列出)?(?:待处理(?:工作|事项|任务)?|待办(?:工作|事项|任务)?)[？?。！!\s]*", text):
            local_work = TerminalIntent(action="pending_work", authorization="approved", confidence="high", user_message=text,
                                        visible_reason="查看当前待办和实际续接条件")
        if local_work is not None and local_work.action == "pending_work":
            scope = active_task_settings.get()
            ticket, plan = Coordinator(project).compile(local_work, collaboration_mode=scope.collaboration_mode,
                                                       task_snapshot_hash=scope.snapshot_hash)
            result = await self._dispatch(project.root, local_work, ticket, plan)
            async with project_write_lock(project.root):
                self._remember_question(project, local_work, ticket, result)
                self._append_dialogue(project, text, local_work, result)
            return {**result, "status": workflow_result_status(result),
                    "session": {"route": "pending_work", "visible_reason": local_work.visible_reason},
                    "pending_work": project.pending_work()}

        # Errors before routing must preserve their original cause, not reference an unbound intent.
        intent = TerminalIntent(action="discuss", user_message=text, authorization="proposed",
                                visible_reason="正在理解本次请求，尚未执行工作流")
        trace = TraceRecorder(project.root, "terminal-session", self.engine.settings.trace_level)
        batch_scopes = AsyncExitStack()
        original_engine = self.engine
        try:
            queued_updates = project.db.get_metadata(conversation_key("pending_terminal_user_updates"), [])
            queued_update_ids = {
                str(item["id"]) for item in queued_updates
                if isinstance(item, dict) and item.get("status") == "pending" and item.get("id")
            } if isinstance(queued_updates, list) else set()
            packet = self._build_packet(project, text)
            proposal_basis = self._work_source(project)
            packet_path = trace.run_dir / "context-packet.md"
            atomic_write_text(packet_path, packet.to_markdown())
            trace.record(
                "session.context",
                "completed",
                "已构建唯一终端会话 Context Packet",
                metadata={"estimated_tokens": packet.estimated_tokens, "sections": [item.key for item in packet.sections]},
            )
            pending = self.pending_resume(project, text)
            if pending is not None:
                saved_intent = TerminalIntent.model_validate(pending["intent"])
                # A normal user often resumes a task and adds one short writing
                # preference in the same sentence. Preserve the saved range and
                # acceptance mode instead of asking Coordinator to infer a new,
                # usually smaller batch from the currently visible drafts.
                added_instruction = text[int(pending["resume_end"]):].strip("。！!，, \t\r\n")
                if added_instruction:
                    previous = saved_intent.operation_instruction.strip()
                    merged = "\n".join(
                        part for part in (
                            previous,
                            f"本次继续时新增要求：{added_instruction}",
                        )
                        if part
                    )[-4_000:]
                    saved_intent = saved_intent.model_copy(
                        update={
                            "operation_instruction": merged,
                            "visible_reason": "继续原批次，并把本次新增的写作要求交给 Writer 与 Editor。",
                        }
                    )
                route_result = ProviderResult(data=saved_intent, model="local-resume", response_id=None, reasoning_content=None, usage={})
                trace.record(
                    "controller.resume",
                    "completed",
                    "从已保存目标继续，无需重新识别或重复确认",
                    metadata={"added_instruction": bool(added_instruction)},
                )
            else:
                local_intent = None if queued_update_ids else (
                    self._confirmed_pending_work_intent(project, text)
                    or (TerminalIntent(action="pending_work", authorization="approved", confidence="high",
                        visible_reason="查看模型可识别的待处理工作和进度。", pending_work_id=None)
                        if re.fullmatch(r"(?:查看|看看|列出|还有什么|处理)?(?:待处理(?:工作|事项|任务)?|待办(?:工作|事项|任务)?)[？?。！!\s]*", text) else None)
                    or self._confirmed_planning_intent(text, self._pending_question(project))
                    or self._confirmed_question_intent(text, self._pending_question(project))
                    or self._deterministic_workflow_intent(text, project=project)
                    or self._deterministic_single_chapter_write(text)
                )
                if local_intent is not None:
                    route_result = ProviderResult(
                        data=local_intent,
                        model="local-intent",
                        response_id=None,
                        reasoning_content=None,
                        usage={},
                    )
                    trace.record(
                        "controller.local_route",
                        "completed",
                        "明确的自然语言请求已由本地规则直接路由",
                        metadata={
                            "action": local_intent.action,
                            "chapter_no": local_intent.chapter_no,
                            "end_chapter_no": local_intent.end_chapter_no,
                        },
                    )
                else:
                    if emit:
                        await emit({"type": "coordinator.model.started", "stage": "controller.routing", "role": "coordinator", "model": self.engine.settings.model, "summary": "墨宝正在理解你的要求"})
                    trace.record_model_started("controller.routing", model=self.engine.settings.model, agent_role="coordinator", max_tokens=1600, thinking=False)
                    route_result = await self.engine.provider.generate_json(
                        system_prompt=TERMINAL_ROUTER_SYSTEM, user_prompt=packet.to_model_prompt(),
                        output_model=TerminalIntent, effort="low", max_tokens=1600, thinking=False, agent_role="coordinator",
                    )
            steering_messages = await consume_steering() if consume_steering else []
            if steering_messages:
                async with project_write_lock(project.root):
                    queued_update_ids.update(self._remember_user_updates(project, steering_messages, "routing"))
                text = self._apply_steering(text, steering_messages)
                packet = self._build_packet(project, text)
                atomic_write_text(packet_path, packet.to_markdown())
                trace.record(
                    "session.steer",
                    "completed",
                    "已在安全节点接收用户引导，并重新判断后续处理。",
                    metadata={"guidance_count": len(steering_messages)},
                )
                if emit:
                    await emit({"type": "coordinator.model.started", "stage": "controller.routing.steer", "role": "coordinator", "model": self.engine.settings.model, "summary": "Coordinator 正在读取新增指导并重新选择工作流"})
                trace.record_model_started(
                    "controller.routing.steer",
                    model=self.engine.settings.model,
                    agent_role="coordinator",
                    max_tokens=1600,
                    timeout_seconds=self.engine.settings.request_timeout_seconds,
                    thinking=False,
                )
                route_result = await self.engine.provider.generate_json(
                    system_prompt=TERMINAL_ROUTER_SYSTEM,
                    user_prompt=packet.to_model_prompt(),
                    output_model=TerminalIntent,
                    effort="low",
                    max_tokens=1600,
                    thinking=False,
                    agent_role="coordinator",
                )
            raw_intent = self._coerce_settings_intent(text, route_result.data)
            from .preferences import capture_observations
            captured_preferences = capture_observations(project.db, raw_intent.preference_observations,
                                                        text, trace.run_id)
            if captured_preferences:
                trace.record("preferences.capture", "completed", "已保存偏好来源；可在设置中查看或调整",
                             metadata={"preferences": captured_preferences})
            if _focused_recent_plan_request(text):
                active_path = project.root / "planning" / "active-v2.json"
                try:
                    active_plan = json.loads(active_path.read_text(encoding="utf-8"))
                    focused = re.search(
                        r"(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)(?:当前|现行|生效)?第\s*(\d+)\s*章",
                        text,
                    )
                    focused_range = re.search(
                        r"(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)(?:当前|现行|生效)?第\s*(\d+)\s*(?:到|至|～|~|—|-)\s*(\d+)\s*章",
                        text,
                    )
                    selected = (list(range(int(focused_range.group(1)), int(focused_range.group(2)) + 1))
                                if focused_range else [int(focused.group(1))] if focused else
                                [int(value) for value in _CHAPTER_NUMBER_PATTERN.findall(text)])
                    first, last = active_plan["chapter_window"]
                    if (active_plan.get("status") == "active" and selected
                            and all(first < number <= last for number in selected)):
                        raw_intent = raw_intent.model_copy(update={
                            "action": "redesign_story", "chapter_no": first,
                            "end_chapter_no": last, "operation_instruction": text,
                            "authorization": "approved" if re.search(
                                r"^(?:墨宝[，,]\s*)?(?:请|现在|继续)", text.strip()
                            ) and not re.search(r"是否|可不可以|要不要|如果这样", text) else raw_intent.authorization,
                            "document_kind": "none", "setting_change": {},
                            "missing_fields": [], "clarification_question": "",
                            "clarification_questions": [],
                            "visible_reason": "只在现行近期规划中修订指定章节；不重做大纲、卷细纲或正史。",
                        })
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            pending_question = self._pending_question(project)
            previous_intent = None
            if pending_question is not None:
                try:
                    previous_intent = TerminalIntent.model_validate(pending_question.get("intent"))
                    previous_intent = previous_intent.model_copy(update={"pending_question_id": pending_question["id"]})
                except (TypeError, ValueError):
                    previous_intent = None
            if raw_intent.response_kind == "question_answer" and pending_question is not None:
                card_ids = {str(card.get("id")) for card in pending_question.get("questions", [])
                            if isinstance(card, dict) and card.get("id")}
                if not raw_intent.pending_question_id or raw_intent.pending_question_id in card_ids:
                    raw_intent = raw_intent.model_copy(update={
                        "pending_question_id": pending_question["id"],
                        "related_task_id": raw_intent.related_task_id or pending_question.get("task_id"),
                    })
                elif raw_intent.pending_question_id == pending_question["id"] and not raw_intent.related_task_id:
                    raw_intent = raw_intent.model_copy(update={"related_task_id": pending_question.get("task_id")})
            elif raw_intent.response_kind == "question_answer":
                raw_intent = raw_intent.model_copy(update={"pending_question_id": None})
            if raw_intent.response_kind == "task_revision" and not raw_intent.related_task_id:
                active_task = project.db.get_metadata(conversation_key("pending_creation_task"))
                if (isinstance(active_task, dict)
                        and active_task.get("status") in {"running", "interrupted", "waiting_condition", "waiting_user"}):
                    related_id = active_task.get("task_id") or active_task.get("run_id")
                    if related_id:
                        raw_intent = raw_intent.model_copy(update={"related_task_id": str(related_id)})
            raw_intent = Coordinator.normalize_intent(raw_intent, text, previous_intent=previous_intent)
            if (pending is None and raw_intent.action in {"batch_draft", "batch_draft_accept", "batch_repair", "batch_accept"}
                    and raw_intent.batch_id and raw_intent.batch_id not in text):
                raw_intent = raw_intent.model_copy(update={"batch_id": None})
            intent, routing_response = self._resolve_intent(project, text, raw_intent)
            preference_items = project.db.effective_preferences() + preference_candidates(project.db)
            preference_decisions = resolve_preference_conflicts(intent.preference_conflicts, preference_items, text)
            prior_question = self._pending_question(project)
            if (prior_question and intent.response_kind == "question_answer"
                    and intent.pending_question_id == prior_question["id"]):
                preference_decisions = prior_question.get("preference_resolutions", []) + preference_decisions
            if preference_decisions:
                record = {"run_id": trace.run_id, "created_at": datetime.now(timezone.utc).isoformat(),
                          "decisions": preference_decisions}
                project.db.set_metadata("preference.decisions:" + trace.run_id, record)
                trace.record("preferences.resolve", "completed", "已核对偏好来源与本次取舍", metadata=record)
                unresolved = [item for item in preference_decisions if item["status"] != "resolved"]
                if unresolved and intent.action not in _READ_ONLY_ACTIONS:
                    question = unresolved[0]
                    options = [{"id": "preference-" + item_id, "label": "本次采用偏好 " + item_id,
                                "description": next((item["text"] for item in preference_items if item["preference_id"] == item_id), "来源待核对"),
                                "kind": "choice"} for item_id in question["preference_ids"]] if question["status"] == "needs_choice" else []
                    routing_response = {"needs_clarification": True, "status": "waiting_user",
                        "reply": "当前适用要求存在待核对的冲突，保留原任务，先确定本次取舍。",
                        "preference_resolutions": [item for item in preference_decisions if item["status"] == "resolved"],
                        "preference_run_id": trace.run_id, "questions": [{"id": "preference-conflict", "header": "习惯取舍",
                            "question": question["reason"], "why_it_matters": "只影响本任务，不修改保存的习惯；来源变更后重新核对。",
                            "selection": "single", "options": options + [{"id": "preference-other", "label": "其他", "kind": "other"}]}]}
                else:
                    batch_scopes.enter_context(use_preference_decisions(preference_decisions))
            if (routing_response is None and intent.authorization == "approved"
                    and intent.action in {"batch_draft", "batch_draft_accept", "batch_repair", "batch_accept"}):
                # Routing belongs to this conversation. Production belongs to the
                # saved batch, including its acceptance policy and model settings.
                bound_engine, manifest = await batch_scopes.enter_async_context(self.engine.batch_operation_async(
                    project.root, intent.action, start_chapter_no=intent.chapter_no,
                    end_chapter_no=intent.end_chapter_no, instruction=intent.operation_instruction,
                    batch_id=intent.batch_id,
                ))
                self.engine = bound_engine
                updates = {"batch_id": manifest["batch_id"]}
                if intent.action in {"batch_draft", "batch_draft_accept"}:
                    updates.update(chapter_no=manifest["start_chapter_no"], end_chapter_no=manifest["end_chapter_no"])
                intent = intent.model_copy(update=updates)
            intent = self._apply_acceptance_policy(text, intent)
            if intent.pending_work_id and intent.action != "pending_work":
                selected_work = next((item for item in project.pending_work() if item["id"] == intent.pending_work_id), None)
                if not isinstance(selected_work, dict) or not selected_work.get("intent"):
                    raise ValidationGateError("待处理任务没有可执行的原任务，请先核对具体问题。")
                if not selected_work.get("can_process"):
                    raise ValidationGateError("此待办状态或恢复条件已变化，已停止重复执行；请查看当前断点。")
                if selected_work["kind"] == "plan_revision" and intent.action in {"review", "revise_review"}:
                    async with project_write_lock(project.root):
                        project.update_pending_work(selected_work["id"], intent=selected_work["intent"],
                            processing_attempts=int(selected_work.get("processing_attempts", 0)) + 1)
                if selected_work.get("kind") == "optimization" and selected_work["source"] != self._work_source(project):
                    project.update_pending_work(intent.pending_work_id, status="waiting_condition",
                        resolution="提出优化之后来源已变化，须按当前版本重新安排。")
                    raise ValidationGateError("优化任务的来源已变化，请重新查看待处理工作，按当前版本安排。")
            task_scope = active_task_settings.get()
            ticket, dispatch_plan = Coordinator(project).compile(
                intent,
                collaboration_mode=task_scope.collaboration_mode if task_scope else "everyday",
                task_snapshot_hash=task_scope.snapshot_hash if task_scope else None,
            )
            Coordinator.validate(dispatch_plan, ticket)
            if queued_update_ids:
                async with project_write_lock(project.root):
                    self._mark_user_updates_routed(project, queued_update_ids)
            # Coordinator estimates do not override the runtime's explicit
            # limits or interrupt a chapter in the middle of normal work.
            trace.record(
                "session.route",
                "completed",
                f"自然语言请求已路由为 {intent.action}",
                details=intent.visible_reason,
                metadata={
                    "action": intent.action,
                    "alternative_action": intent.alternative_action,
                    "confidence": intent.confidence,
                    "authorization": intent.authorization,
                    "missing_fields": intent.missing_fields,
                    "chapter_no": intent.chapter_no,
                    "model": route_result.model,
                    "response_id": route_result.response_id,
                    "usage": route_result.usage,
                },
            )
            if intent.authorization == "approved" and dispatch_plan.steps:
                async with project_write_lock(project.root):
                    for step in dispatch_plan.steps:
                        if step.role == "engine":
                            continue
                        project.db.append_collaboration_message(
                            thread_id=ticket.ticket_id,
                            run_id=trace.run_id,
                            sender_role="coordinator",
                            recipient_role=step.role,
                            message_type="task_assignment",
                            chapter_no=ticket.chapter_no,
                            chapter_version=ticket.chapter_version,
                            context_packet_id=content_hash(packet.to_model_prompt()),
                            claim=ticket.objective,
                            evidence_refs=ticket.input_sources,
                            requested_response=step.required_output,
                        )
            if routing_response is not None:
                response = routing_response
            elif intent.action == "discuss":
                response: dict[str, Any] = {
                    "reply": intent.conversation_reply.strip()
                    or "我已理解你的想法。请确认要继续讨论，还是让我按这个方向执行下一步。"
                }
                if intent.clarification_questions or intent.clarification_question.strip():
                    response.update(
                        {
                            "needs_clarification": True,
                            "questions": self._question_cards(intent),
                        }
                    )
            elif intent.action == "chat":
                # 闲聊不进工作流：路由模型已按墨宝口吻写出完整回复，这里只兜底空回复。
                response: dict[str, Any] = {
                    "reply": intent.conversation_reply.strip()
                    or "我在呢。想聊点什么，或者继续你的故事都可以。"
                }
            elif intent.action == "ideate":
                # Writer 灵感分身：零依据构思不做证据核验，产出提案不入正史。
                if intent.authorization != "approved":
                    response = {
                        "gate": "想让我出点子的话，直接说就行，例如“帮我想几个故事点子”。"
                    }
                else:
                    response = await self.engine.brainstorm(project.root, text, packet)
            else:
                if intent.authorization == "approved" and intent.action in _PENDING_TASK_ACTIONS:
                    async with project_write_lock(project.root):
                        self._save_pending_task(project, intent, "running")
                async def tracked_steering() -> list[str]:
                    updates = await consume_steering() if consume_steering else []
                    if updates:
                        async with project_write_lock(project.root):
                            self._remember_user_updates(project, updates, ticket.ticket_id)
                    return updates

                response = await self._dispatch(
                    project.root, intent, ticket, dispatch_plan,
                    packet=packet,
                    consume_steering=tracked_steering if consume_steering else None,
                )
                response = await self._recover_failed_workflow(project, intent, ticket, dispatch_plan, response, trace)
                self._planning_question(response)
                stack = [response]
                review_conflicts = []
                while stack:
                    value = stack.pop()
                    if isinstance(value, dict):
                        if value.get("preference_conflicts"):
                            review_conflicts = value["preference_conflicts"]
                            break
                        stack.extend(item for item in value.values() if isinstance(item, (dict, list)))
                    elif isinstance(value, list):
                        stack.extend(value)
                if review_conflicts:
                    from .schemas import PreferenceConflict
                    conflicts = [PreferenceConflict.model_validate(item) for item in review_conflicts][:3]
                    decisions = resolve_preference_conflicts(conflicts,
                        project.db.effective_preferences() + preference_candidates(project.db), intent.operation_instruction)
                    project.db.set_metadata("preference.decisions:" + trace.run_id,
                        {"run_id": trace.run_id, "created_at": datetime.now(timezone.utc).isoformat(), "decisions": decisions})
                    question = next((item for item in decisions if item["status"] != "resolved"), decisions[0])
                    options = [{"id": "preference-" + item_id, "label": "本次采用偏好 " + item_id,
                        "description": quote, "kind": "choice"} for item_id, quote in zip(question["preference_ids"], question["source_quotes"])]
                    resume_action = {"write_review": "review", "revise_review": "review",
                        "write_review_accept": "review_accept", "revise_review_accept": "review_accept"}.get(intent.action, intent.action)
                    intent = intent.model_copy(update={"action": resume_action, "preference_conflicts": conflicts})
                    response.update(status="waiting_user", needs_clarification=True, preference_run_id=trace.run_id,
                        preference_resolutions=list(active_preference_decisions.get()) + [item for item in decisions if item["status"] == "resolved"],
                        questions=[{"id": "preference-conflict", "header": "习惯取舍", "question": question["reason"],
                            "why_it_matters": "保留当前稿件与原任务配置，仅核对冲突偏好的本次使用。",
                            "selection": "single", "options": options + [{"id": "preference-other", "label": "其他", "kind": "other"}]}])
                if intent.authorization == "approved" and intent.action in _PENDING_TASK_ACTIONS:
                    async with project_write_lock(project.root):
                        outcome = workflow_result_status(response)
                        pending_status = "completed" if outcome == "completed" else (
                            "waiting_user" if outcome == "waiting_user" else "interrupted"
                        )
                        self._save_pending_task(project, intent, pending_status)
            if intent.authorization == "approved" and dispatch_plan.steps:
                async with project_write_lock(project.root):
                    project.db.resolve_collaboration_thread(ticket.ticket_id)
                    failure = workflow_failure_reason(response)
                    if failure or response.get("needs_clarification"):
                        project.db.append_collaboration_message(
                            thread_id=ticket.ticket_id,
                            run_id=trace.run_id,
                            sender_role="coordinator",
                            recipient_role="user",
                            message_type="risk",
                            chapter_no=ticket.chapter_no,
                            chapter_version=ticket.chapter_version,
                            context_packet_id=content_hash(packet.to_model_prompt()),
                            claim=failure or "任务仍需要用户补充信息。",
                            evidence_refs=ticket.input_sources,
                            requested_response="请补充阻塞信息或确认新的处理方向。",
                            status="escalated",
                        )
            async with project_write_lock(project.root):
                if intent.pending_work_id and intent.action != "pending_work":
                    work_status = workflow_result_status(response)
                    selected_work = next((item for item in project.pending_work() if item["id"] == intent.pending_work_id), {})
                    if selected_work.get("kind") == "accepted_repair" and work_status == "completed":
                        work_status = "waiting_user"
                    project.update_pending_work(intent.pending_work_id,
                        status=work_status,
                        progress={"status": workflow_result_status(response), "action": intent.action,
                                  "steps": [{"step": step.get("step"), "status": workflow_result_status(step),
                                             "reason": workflow_failure_reason(step)} for step in response.get("steps", [])]},
                        resolution_run_id=trace.run_id)
                model_input = packet.to_model_prompt()
                for proposal in intent.pending_work_proposals:
                    if proposal.action in {"pending_work", "exit", "settings_update", "rollback_restore",
                                          "accept", "batch_accept", "planning_history_restore", "planning_publish_reviewed"}:
                        continue
                    if proposal.evidence not in model_input or re.search(r"(?:不要|禁止|不允许|无需).{0,8}额外.{0,8}(?:调用|任务|优化)", text):
                        continue
                    if self._work_source(project) != proposal_basis:
                        continue  # A proposal grounded in the old input cannot bind the new result.
                    proposed_intent = TerminalIntent(action=proposal.action, chapter_no=proposal.chapter_no,
                        end_chapter_no=proposal.end_chapter_no, outline_level=proposal.outline_level,
                        operation_instruction=proposal.instruction, requested_outcome=proposal.reason,
                        visible_reason=proposal.reason[:240], authorization="proposed", confidence="medium")
                    project.record_pending_work(kind="optimization", reason=proposal.reason,
                        next_action=proposal.instruction, source=proposal_basis,
                        intent=proposed_intent.model_dump(mode="json"), status="awaiting_confirmation",
                        chapter_no=proposal.chapter_no)
                project.reconcile_pending_work(current_pass=lambda number: self.engine.current_pass_review(project, number),
                                               current_source=self._work_source(project))
                if intent.action == "pending_work" and intent.pending_work_decision == "inspect":
                    self._pending_work_question(project, response)
                self._remember_question(project, intent, ticket, response)
            if captured_preferences:
                response["preference_updates"] = captured_preferences
                response["reply"] = str(response.get("reply", "")) + "\n\n已记录写作偏好或待确认反馈，可在设置 → 作者习惯与记忆中查看、调整。"
            self._append_dialogue(project, text, intent, response)
            failure = workflow_failure_reason(response)
            outcome = workflow_result_status(response)
            runtime = active_runtime.get()
            if runtime:
                for work in project.pending_work():
                    if work["kind"] == "workflow" and work.get("run_id") == runtime.run_id:
                        project.update_pending_work(work["id"], resolution=failure or response.get("next_action", ""),
                            progress={"status": outcome, "action": intent.action,
                                      "steps": [{"step": step.get("step"), "status": workflow_result_status(step),
                                                 "reason": workflow_failure_reason(step)} for step in response.get("steps", [])]})
            trace.record("session.dispatch", outcome, failure or "受限工作流已返回结果")
            trace.finish(status=outcome, summary=failure or "终端自然语言请求处理完成")
            payload = {
                "status": outcome,
                "pending_work": project.pending_work(),
                "session": {
                    "route": intent.action,
                    "requested_outcome": intent.requested_outcome,
                    "alternative_action": intent.alternative_action,
                    "confidence": intent.confidence,
                    "authorization": intent.authorization,
                    "chapter_no": intent.chapter_no,
                    "visible_reason": intent.visible_reason,
                    "task_ticket": ticket.model_dump(mode="json"),
                    "dispatch_plan": dispatch_plan.model_dump(mode="json"),
                    "role_capabilities": Coordinator.capabilities(),
                    "trace_id": trace.run_id,
                    "trace_path": str(trace.trace_path),
                },
                **response,
            }
            payload["status"] = outcome
            payload["pending_work"] = project.pending_work()
            recommendation = self._recommend_next_step(intent, response)
            if recommendation:
                payload["next_step"] = recommendation
            return payload
        except asyncio.CancelledError:
            try:
                self._mark_current_pending_task_interrupted(project)
            except Exception as exc:
                try:
                    trace.record("session.recovery", "waiting_condition", "无法更新任务断点状态", str(exc))
                except Exception:
                    pass
            self._append_dialogue_entry(project, text, "任务已停止，已有内容保留。", "用户停止或运行中断")
            trace.record("session", "cancelled", "当前请求被停止；没有提交新的正文或正史")
            trace.finish(status="cancelled", summary="终端任务已停止，已有项目内容保留")
            raise
        except ProjectBusyError as exc:
            # A competing project's writer is a temporary condition, not a
            # failed novel. Do not replay the entire natural-language request:
            # earlier steps may already have persisted their own results.
            if intent.authorization == "approved" and intent.action in _PENDING_TASK_ACTIONS:
                async with project_write_lock(project.root):
                    self._save_pending_task(project, intent, "waiting_condition")
            trace.record("session", "waiting_condition", "项目写入暂时被其他任务占用", str(exc))
            trace.finish(status="waiting_condition", summary="等待项目写入空闲；已有成果保留")
            return {
                "session": {"trace_id": trace.run_id, "trace_path": str(trace.trace_path)},
                "status": "waiting_condition",
                "reply": str(exc),
                "next_action": "待占用任务结束后先核对已保存的章节和审查，再续接未完成步骤；不会自动重发整条对话。",
            }
        except PlanningNeedsAttention as exc:
            planning_response = {"step": exc.recovery_node or "planning.dependencies", "gate": str(exc),
                "status": "waiting_condition", "error_type": type(exc).__name__, "next_action": exc.next_action,
                "recovery_node": exc.recovery_node, "recovery_attempts": exc.recovery_attempts,
                "recovery_exhausted": exc.recovery_exhausted}
            if intent.authorization == "approved" and intent.action in _PENDING_TASK_ACTIONS:
                async with project_write_lock(project.root):
                    self._save_pending_task(project, intent, "waiting_condition")
            if intent.pending_work_id:
                project.update_pending_work(intent.pending_work_id, status="waiting_condition", resolution=str(exc))
            runtime = active_runtime.get()
            if runtime:
                for work in project.pending_work():
                    if work.get("run_id") == runtime.run_id:
                        project.update_pending_work(work["id"], resolution=str(exc), next_action=exc.next_action)
            if "ticket" in locals() and "dispatch_plan" in locals():
                try:
                    planning_response = await self._recover_failed_workflow(project, intent, ticket, dispatch_plan,
                                                                            planning_response, trace)
                except Exception as recovery_exc:
                    planning_response["recovery_error"] = str(recovery_exc)
            planning_outcome = workflow_result_status(planning_response)
            self._append_dialogue_entry(project, text, str(exc), "规划保留，低风险多节点补救按实际结果续接")
            trace.record("session", planning_outcome, "规划依赖与协调恢复结果已记录", str(exc))
            trace.finish(status=planning_outcome, summary=str(exc))
            if planning_outcome == "completed" and intent.action in _PENDING_TASK_ACTIONS:
                self._save_pending_task(project, intent, "completed")
            return planning_response | {
                "session": {"trace_id": trace.run_id, "trace_path": str(trace.trace_path)},
                "status": planning_outcome, "reply": "已从故障节点恢复，原任务完成。" if planning_outcome == "completed" else str(exc),
                "next_action": planning_response.get("next_action") or exc.next_action,
            }
        except InkFlowError as exc:
            failure_response = {"step": exc.recovery_node or getattr(locals().get("intent"), "action", "workflow"),
                "gate": str(exc), "status": "waiting_condition", "error_type": type(exc).__name__,
                "recovery_node": exc.recovery_node, "recovery_attempts": exc.recovery_attempts,
                "recovery_exhausted": exc.recovery_exhausted}
            try:
                self._mark_current_pending_task_failed(project)
                if intent.pending_work_id:
                    project.update_pending_work(intent.pending_work_id, status="failed", resolution=str(exc))
                runtime = active_runtime.get()
                if runtime:
                    for work in project.pending_work():
                        if work.get("run_id") == runtime.run_id:
                            project.update_pending_work(work["id"], resolution=str(exc), reason=str(exc))
            except Exception as recovery_exc:
                trace.record("session.recovery", "waiting_condition", "无法更新失败任务状态", str(recovery_exc))
            if "ticket" in locals() and "dispatch_plan" in locals():
                try:
                    failure_response = await self._recover_failed_workflow(project, intent, ticket, dispatch_plan, failure_response, trace)
                    if workflow_result_status(failure_response) == "completed" and intent.action in _PENDING_TASK_ACTIONS:
                        self._save_pending_task(project, intent, "completed")
                except Exception as recovery_exc:
                    failure_response["recovery_error"] = str(recovery_exc)
            recovery_outcome = workflow_result_status(failure_response)
            self._append_dialogue_entry(project, text,
                "已从受影响节点恢复，实际产物通过原任务门禁。" if recovery_outcome == "completed" else str(exc),
                "多节点恢复已核验" if recovery_outcome == "completed" else "任务未完成，已有内容保留")
            trace.record("session", recovery_outcome, "故障节点与恢复结果已记录", str(exc))
            trace.finish(status=recovery_outcome, summary="原任务按实际恢复结果保留进度")
            return failure_response | {
                "session": {"trace_id": trace.run_id, "trace_path": str(trace.trace_path)},
                "status": recovery_outcome,
                "next_action": failure_response.get("next_action") or "查看待处理工作的具体原因、正文和报告版本，再从受影响节点继续。",
            }
        except Exception as exc:
            try:
                self._mark_current_pending_task_failed(project)
            except Exception as recovery_exc:
                trace.record("session.recovery", "waiting_condition", "无法更新失败任务状态", str(recovery_exc))
            self._append_dialogue_entry(project, text, "运行遇到异常，已有内容保留；请查看任务提示。", "任务异常")
            trace.record("session", "failed", "终端会话发生未预期错误", str(exc))
            trace.finish(status="failed", summary="终端自然语言请求失败")
            raise
        finally:
            self.engine = original_engine
            await batch_scopes.aclose()

    @staticmethod
    def _recovery_nodes(response: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect workflow receipts, not novel prose or model success claims."""
        nodes = []
        stack = [response]
        while stack:
            value = stack.pop()
            if not isinstance(value, dict):
                continue
            if value.get("step") or value.get("recovery_exhausted"):
                nodes.append({key: value.get(key) for key in
                    ("step", "status", "error_type", "recovery_node", "recovery_attempts", "recovery_exhausted", "next_action")}
                    | {"status": workflow_result_status(value), "reason": workflow_failure_reason(value),
                       "result": {key: value.get("result", {}).get(key) for key in
                           ("chapter_no", "version", "source_hash", "content_hash", "path", "trace_id", "verdict")}
                           if isinstance(value.get("result"), dict) else {}})
            stack.extend(value.get("steps", []) if isinstance(value.get("steps"), list) else [])
            if isinstance(value.get("result"), dict):
                stack.append(value["result"])
        return nodes

    @staticmethod
    def _validate_recovery_step(step: Any, original: TerminalIntent, boundary: int) -> TerminalIntent | None:
        """Whitelist only same-task diagnostics and reviews; never write or accept."""
        if step.action in {"diagnose_dependencies", "ask_user", "development_fix"}:
            return None
        if "review" in original.forbidden_actions:
            raise ValidationGateError("原任务禁止审查，恢复计划不能扩大权限。")
        start, end = step.chapter_no, step.end_chapter_no or step.chapter_no
        if not start or not end or not (1 <= start <= end <= boundary):
            raise ValidationGateError("恢复复核超出本任务及其边界内依赖章节。")
        if step.action == "review" and start != end:
            raise ValidationGateError("单章复核不能暗中扩成批次。")
        if step.action == "batch_review" and (not original.batch_id or not original.chapter_no
                or start < original.chapter_no or end > (original.end_chapter_no or original.chapter_no)):
            raise ValidationGateError("临时补审必须属于原批次与原授权范围。")
        return original.model_copy(update={"action": "batch_repair" if step.action == "batch_review" else step.action,
            "chapter_no": start, "end_chapter_no": end,
            "batch_review_only": step.action == "batch_review", "max_revision_rounds": 0,
            "operation_instruction": step.instruction or original.operation_instruction,
            "pending_work_proposals": [], "preference_observations": [], "preference_conflicts": []})

    @staticmethod
    def _store_recovery_record(project: InkFlowProject, key: str, record: dict[str, Any]) -> None:
        # Late results may not erase a concurrently reserved second attempt.
        with project_write_lock_sync(project.root):
            saved = project.db.get_metadata(key, {})
            attempts = {item["number"]: item for item in saved.get("attempts", [])}
            for item in record.get("attempts", []):
                if item["status"] != "started" or attempts.get(item["number"], {}).get("status", "started") == "started":
                    attempts[item["number"]] = item
            same_source = saved.get("source") == record.get("source")
            record["attempts"] = [attempts[number] for number in sorted(attempts)]
            if same_source:
                record["completed_nodes"] = sorted(set(saved.get("completed_nodes", [])) | set(record.get("completed_nodes", [])))
            if (record.get("status") == "waiting_condition" and len(record["attempts"]) >= 2
                    and all(item["status"] != "started" for item in record["attempts"])):
                record["status"] = "waiting_user"
            project.db.set_metadata(key, record)

    async def _recover_failed_workflow(self, project: InkFlowProject, intent: TerminalIntent,
            ticket: TaskTicket, dispatch: DispatchPlan, response: dict[str, Any], trace: TraceRecorder) -> dict[str, Any]:
        outcome = workflow_result_status(response)
        if intent.action == "pending_work" or outcome not in {"failed", "waiting_condition"} or response.get("questions"):
            return response
        no_extra = bool(set(intent.forbidden_actions) & {"extra_calls", "extra_model_calls", "coordinator_recovery"})
        no_extra = no_extra or bool(re.search(r"(?:不要|禁止|不允许|无需).{0,8}额外.{0,8}(?:调用|模型|任务)",
                                               intent.user_message + "\n" + intent.operation_instruction))
        if no_extra:
            return response | {"coordinator_recovery": {"status": "not_run", "reason": "原任务禁止额外调用，保留故障节点与已有成果。"}}
        runtime, scope = active_runtime.get(), active_task_settings.get()
        if not runtime or not scope or intent.authorization != "approved" or runtime.task_id != scope.task_id:
            return response
        if dispatch.task_snapshot_hash != scope.snapshot_hash:
            raise ValidationGateError("故障任务的原配置快照不一致，不能换当前配置恢复。")
        nodes = self._recovery_nodes(response)
        failed = next((item for item in nodes if item["status"] != "completed"), {})
        node = str(failed.get("recovery_node") or failed.get("step") or dispatch.workflow)
        # Counts bind task/node, never error wording, prose version or source hash.
        key = f"coordinator_recovery:{scope.task_id}:{node}"
        record = project.db.get_metadata(key, {})
        if record and record.get("snapshot_hash") != scope.snapshot_hash:
            raise ValidationGateError("原故障恢复记录的配置身份不一致，需核对原快照。")
        history = list(record.get("attempts", []))
        exhausted = any(item.get("recovery_exhausted") or (item.get("recovery_attempts") or 0) >= 2 for item in nodes)
        reason = workflow_failure_reason(response) or "必要审查或依赖交接尚未完成。"
        source = self._work_source(project)
        case = {"task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash, "node": node,
            "original_intent": intent.model_dump(mode="json"), "ticket": ticket.model_dump(mode="json"),
            "mode": dispatch.collaboration_mode, "steps": nodes, "failure": reason,
            "source": source, "previous_attempts": history, "runtime": runtime.snapshot()}
        latest_review = project.db.latest_review_record(intent.chapter_no) if intent.chapter_no else None
        current_chapter = project.db.get_chapter(intent.chapter_no) if intent.chapter_no else None
        same_round_recovery = bool(latest_review and current_chapter
            and latest_review["chapter_version"] == current_chapter["version"]
            and latest_review["report"].source_hash == current_chapter["content_hash"]
            and latest_review["report"].evidence_recovery.get("attempted"))
        if same_round_recovery and node == "engine.review_mode":
            evidence = latest_review["report"].evidence_recovery
            reason = "同版审查已完成有界补读，仍有必要依据缺口，已停止外围重审。"
            detail = evidence.get("error") or "；".join(evidence.get("remaining_gaps", []))
            if not detail:
                detail = "；".join(str(value.get("error") or "；".join(value.get("remaining_gaps", [])))
                    for value in evidence.get("roles", {}).values()
                    if value.get("error") or value.get("remaining_gaps"))
            if detail:
                reason += "原因：" + str(detail)
            return response | {"status": "waiting_condition", "reply": reason,
                "coordinator_recovery": {"status": "not_run", "reason": reason},
                "next_action": "查看本章当前报告和补读失败记录，修复具体来源或模型输出条件后，从原责任节点续接。"}
        case["same_round_evidence_recovery_used"] = same_round_recovery
        boundary = intent.end_chapter_no or intent.chapter_no or max(
            [project.db.latest_accepted_chapter_no(), *project.db.chapter_numbers_by_status("draft")], default=0)
        done = set(record.get("completed_nodes", [])) if record.get("source") == source else set()
        results = []
        while len(history) < 2 and not exhausted:
            packet = self._build_packet(project, intent.user_message or intent.operation_instruction)
            case["previous_attempts"] = history
            case["runtime"] = runtime.snapshot()
            case["same_round_evidence_recovery_used"] = same_round_recovery
            packet.sections.append(ContextSection(key="FAILURE", title="原任务多节点故障与恢复记录",
                content=json_dumps(case), hard=True, cache_scope="request"))
            prompt = packet.to_model_prompt()
            needed = estimate_tokens(prompt + COORDINATOR_RECOVERY_SYSTEM) + 1800
            if (runtime.calls >= runtime.max_calls or (runtime.max_tokens is not None and runtime.tokens + needed > runtime.max_tokens)
                    or estimate_tokens(prompt) > self.engine.settings.context_budget_for("coordinator")[1]):
                reason = "剩余预算或Coordinator上下文不足，恢复进度保留，未增加调用。"
                break
            attempt = {"number": len(history) + 1, "run_id": runtime.run_id, "status": "started", "nodes": []}
            executed = []
            async with project_write_lock(project.root):
                current = project.db.get_metadata(key, {})
                history = list(current.get("attempts", history))
                if len(history) >= 2:
                    break
                if current.get("source") == source:
                    done.update(current.get("completed_nodes", []))
                attempt["number"] = len(history) + 1
                history.append(attempt)
                record = {"task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash, "node": node,
                    "attempts": history, "completed_nodes": sorted(done), "failure_case": case, "source": source}
                project.db.set_metadata(key, record)  # Persist before any remote request; interruptions count.
            try:
                trace.record_model_started("coordinator.recovery", model=self.engine.settings.model,
                    agent_role="coordinator", max_tokens=1800, thinking=False)
                planned = await self.engine.provider.generate_json(system_prompt=COORDINATOR_RECOVERY_SYSTEM,
                    user_prompt=prompt, output_model=CoordinatorRecoveryPlan, effort="low", max_tokens=1800,
                    thinking=False, agent_role="coordinator")
                trace.record_model("coordinator.recovery", planned, "失败后提出受原任务约束的多节点补救计划")
                for step in planned.data.steps:
                    if step.evidence not in prompt:
                        raise ValidationGateError("恢复动作的依据未定位到本次故障输入。")
                    if active_task_settings.get() != scope or self._work_source(project) != source:
                        raise ValidationGateError("恢复期间来源或原快照变化，须按当前版本定点重核。")
                    signature = f"{step.action}:{step.chapter_no}:{step.end_chapter_no or step.chapter_no}"
                    if signature in done:
                        attempt["nodes"].append({"action": signature, "status": "reused", "reason": "已完成节点不重放"})
                        continue
                    action = self._validate_recovery_step(step, intent, boundary)
                    if (same_round_recovery and step.action in {"review", "arc_audit", "batch_review"}
                            and step.chapter_no <= intent.chapter_no <= (step.end_chapter_no or step.chapter_no)):
                        raise ValidationGateError("同版报告已经补读复核，不能在外围再做整份重审；保留原结果并定位尚缺节点。")
                    if step.action in {"ask_user", "development_fix"}:
                        project.record_pending_work(kind="development_repair" if step.action == "development_fix" else "recovery_decision",
                            reason=step.reason, next_action=step.instruction or step.reason,
                            source={"task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash, "node": node,
                                    "failure_case": case}, status="waiting_user", run_id=runtime.run_id)
                        attempt["nodes"].append({"action": signature, "status": "waiting_user", "reason": step.reason})
                        reason = step.reason
                        break
                    if action is None:
                        result = {"result": self.engine.status(project.root)}
                    else:
                        if step.action != "batch_review":
                            for number in range(int(action.chapter_no), int(action.end_chapter_no) + 1):
                                row = project.db.get_chapter(number)
                                if not row or (number < (intent.chapter_no or boundary) and row["status"] != "accepted"):
                                    raise ValidationGateError("依赖复核只能读取已有且边界可信的正文，不能生成缺失章。")
                        new_ticket, new_dispatch = Coordinator(project).compile(action,
                            collaboration_mode=scope.collaboration_mode, task_snapshot_hash=scope.snapshot_hash)
                        Coordinator.validate(new_dispatch, new_ticket)
                        result = await self._dispatch(project.root, action, new_ticket, new_dispatch)
                    status = workflow_result_status(result)
                    receipt = {"action": signature, "status": status, "reason": workflow_failure_reason(result),
                               "nodes": self._recovery_nodes(result)}
                    attempt["nodes"].append(receipt)
                    results.append(result)
                    executed.append((signature, status, result))
                    latest = project.db.latest_review_record(intent.chapter_no) if intent.chapter_no else None
                    current = project.db.get_chapter(intent.chapter_no) if intent.chapter_no else None
                    same_round_recovery = bool(latest and current
                        and latest["chapter_version"] == current["version"]
                        and latest["report"].source_hash == current["content_hash"]
                        and latest["report"].evidence_recovery.get("attempted"))
                    if status != "completed":
                        reason = receipt["reason"] or "必要复核仍未通过，保留原稿与断点。"
                        break
                    done.add(signature)
                    record.update(completed_nodes=sorted(done), attempts=history)
                    self._store_recovery_record(project, key, record)
                # Re-check the actual current pass gate, never the plan's summary.
                reviewed = self._reuse_current_pass_review(project, intent.chapter_no) if intent.chapter_no else None
                goal_ready = node in {"engine.review_mode", "memory.accept"}
                if intent.action.startswith(("write_", "revise_")) and intent.chapter_no:
                    goal_ready = goal_ready and self._reuse_workflow_writer(project, intent.chapter_no,
                        "write" if intent.action.startswith("write_") else "revise", intent.operation_instruction) is not None
                waiting_decision = any(item["status"] == "waiting_user" for item in attempt["nodes"])
                if not waiting_decision and intent.action in {"arc_audit", "batch_repair"}:
                    expected_action = "batch_review" if intent.action == "batch_repair" and intent.batch_review_only else intent.action
                    expected = f"{expected_action}:{intent.chapter_no}:{intent.end_chapter_no or intent.chapter_no}"
                    completed = next((result for action_key, status, result in executed
                        if action_key == expected and status == "completed"), None)
                    if completed is not None:
                        attempt["status"] = "completed"
                        record.update(attempts=history, completed_nodes=sorted(done), status="completed")
                        self._store_recovery_record(project, key, record)
                        return completed | {"coordinator_recovery": record}
                if reviewed and goal_ready and not waiting_decision and intent.action in {"review", "review_accept", "write_review", "write_review_accept", "revise_review", "revise_review_accept"}:
                    preserved = [item for item in response.get("steps", []) if workflow_result_status(item) == "completed"]
                    repaired = {"steps": [*preserved, reviewed]}
                    if intent.action.endswith("_accept") and "accept" not in intent.forbidden_actions:
                        repaired = await self._conditionally_accept(project.root, intent.chapter_no, repaired["steps"], reviewed)
                    attempt["status"] = workflow_result_status(repaired)
                    if attempt["status"] == "completed":
                        record.update(attempts=history, completed_nodes=sorted(done), status="completed")
                        self._store_recovery_record(project, key, record)
                        return repaired | {"coordinator_recovery": record | {"initial_failure": case["failure"]}}
                attempt["status"] = "waiting_user" if any(item["status"] == "waiting_user" for item in attempt["nodes"]) else "failed"
            except asyncio.CancelledError:
                attempt["status"] = "interrupted"
                self._store_recovery_record(project, key, record)
                self._mark_current_pending_task_interrupted(project)
                raise
            except RunBudgetExceeded as exc:
                attempt.update(status="waiting_condition", reason=str(exc), error_type=type(exc).__name__)
                reason = str(exc)
            except Exception as exc:
                attempt.update(status="failed", reason=str(exc), error_type=type(exc).__name__)
                reason = str(exc)
            self._store_recovery_record(project, key, record)
            trace.record("coordinator.recovery", attempt["status"], "Coordinator多节点恢复已记录", reason,
                metadata={"node": node, "attempt": attempt["number"]})
            if attempt["status"] in {"waiting_user", "waiting_condition"} or self._work_source(project) != source:
                break
        record.update(task_id=scope.task_id, snapshot_hash=scope.snapshot_hash, node=node,
            attempts=history, completed_nodes=sorted(done), failure_case=case, source=source,
            status="waiting_user" if len(history) >= 2 or exhausted else "waiting_condition")
        self._store_recovery_record(project, key, record)
        next_action = ("自动恢复已达到两次上限，请先核对动作结果并修复来源或交开发处理，再按原任务定点续接。"
                       if len(record["attempts"]) >= 2 or exhausted else
                       "当前依赖或预算尚未满足，请核对动作结果后定点续接。")
        next_action += "已完成正文和提交不重放，源码缺陷不在小说运行时自动修改。"
        work = project.record_pending_work(kind="coordinator_recovery", reason=reason, next_action=next_action,
            source={"task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash, "node": node, "recovery_record_key": key},
            status=record["status"], run_id=runtime.run_id)
        project.update_pending_work(work["id"], progress={"node": node, "attempts": record["attempts"],
            "underlying_recovery_exhausted": exhausted, "original_node_receipts": nodes,
            "status": record["status"]})
        return response | {"coordinator_recovery": record, "recovery_results": results,
            "status": record["status"], "next_action": next_action}

    def _quick_response(self, project: InkFlowProject, text: str) -> dict[str, Any] | None:
        """Avoid a paid model routing call for unambiguous read-only requests."""

        normalized = re.sub(r"\s+", "", text).lower()
        if normalized in {"状态", "项目状态", "当前状态", "当前进度", "进度"}:
            return {
                "session": {"route": "status", "cost": "无需模型调用"},
                "result": self.engine.status(project.root),
                "next_step": {
                    "label": "让 Coordinator 推荐下一步",
                    "reason": "状态已经读完；你可以让 Coordinator 根据当前进度选择最合适的动作。",
                    "prompt": "根据当前项目状态，给我推荐下一步并说明原因",
                },
            }
        if normalized in {"帮助", "help", "/help", "怎么用"}:
            return {
                "session": {"route": "help", "cost": "无需模型调用"},
                "help": [
                    "直接按平常说话表达目标，不需要记住固定关键词；有歧义时我只追问缺少的部分。",
                    "先讨论：例如“我想把男主改成双主角，先分析利弊”。",
                    "讨论后可以说“行，就照刚才那个方向写第 3 章，先别审查”。",
                    "需要验收时再说“审查第 3 章”；只有明确要求入正史才会接收。",
                    "批量草稿完成后可集中查看；接收批次后才会写入正史。",
                    "篇章结束时可说“复审第 1 到第 10 章并和规划对比”；它只产出报告。",
                ],
            }
        return None

    def _deterministic_workflow_intent(
        self, text: str, *, project: InkFlowProject | None = None,
    ) -> TerminalIntent | None:
        """Route an explicit chapter range without paying a model to restate it.

        Keep this deliberately narrow.  Ambiguous conversation still belongs
        to Coordinator; an imperative that names a concrete range, asks for
        prose, and states whether it should be accepted does not.
        """

        from_to_matches = list(_CHAPTER_FROM_TO_PATTERN.finditer(text))
        range_matches = from_to_matches or list(_CHAPTER_RANGE_PATTERN.finditer(text))
        # "接着写到20章" is a complete goal when the active project's next
        # canonical chapter supplies the start. Other ambiguous ranges still
        # belong to Coordinator rather than a fabricated chapter number.
        if len(range_matches) == 1:
            start_chapter, end_chapter = (int(value) for value in range_matches[0].groups())
        elif len(range_matches) == 0 and project is not None:
            target = re.search(r"(?:继续|接着|往下|一路|一直)[^。！？；\n]{0,16}(?:写|续写|创作|完成)(?:到|至)第?\s*(\d+)\s*章", text)
            if not target or re.search(r"先聊|只讨论|先别写|不写|不要写|能不能|可不可以|为什么|怎么", text):
                return None
            start_chapter = project.db.latest_accepted_chapter_no() + 1
            end_chapter = int(target.group(1))
        else:
            return None
        if start_chapter < 1 or end_chapter <= start_chapter:
            return None

        # A chapter range can delimit a planning document.  Treat an explicit
        # outline request as such before looking for incidental words like
        # "已写好的正文"; otherwise a harmless revision can start a full batch.
        deferred_detail = bool(
            re.search(r"(?:这次|现在|先|目前)只(?:做|改|生成|修订|整理|重构)[^。！？\n]{0,24}大纲", text)
            and re.search(r"(?:同意|确认|看过|审读)[^。！？\n]{0,24}后[^。！？\n]{0,24}细纲", text)
        )
        asks_outline = (
            "大纲" in text and ("细纲" not in text or deferred_detail)
            and re.search(
                r"(?:生成|整理|重做|重构|重排|修订|修改|调整|优化|重新规划|完善)[^。！？\n]{0,30}大纲"
                r"|大纲[^。！？\n]{0,30}(?:生成|整理|重做|重构|重排|修订|修改|调整|优化|重新规划|完善)",
                text,
            ) is not None
        )
        if asks_outline:
            if re.search(r"(?:为什么|怎么|如何|是否|能不能|可不可以)[^。！？\n]*[？?]?$", text):
                return None
            if re.search(r"(?:不|别|不要|先别)[^。！？\n]{0,8}(?:改|修改|调整|生成)[^。！？\n]{0,8}大纲", text):
                return None
            return TerminalIntent(
                action="outline", outline_level="story", requested_outcome=text[:1_000],
                confidence="high", authorization="approved", authorization_source="current_request",
                chapter_no=start_chapter, end_chapter_no=end_chapter,
                operation_instruction=text[-4_000:],
                visible_reason=f"只修订第 {start_chapter}～{end_chapter} 章大纲，不生成正文或近期计划。",
            )

        # Mentioning a plan as a writing source is not a request to regenerate
        # it. Only an explicit planning edit delegates this range to routing.
        if re.search(
            r"(?:重做|重构|重写|重新设计|重新规划|重新整理|修改|调整|修订|生成|整理|完善)"
            r"[^。！？；\n]{0,24}(?:大纲|细纲|近期规划|章节规划|章节卡)", text,
        ):
            return None

        # A range can describe a planning/editing window, not a request to
        # write chapters.  Never let incidental words such as "已写正文" or
        # "完成卷末收束" override an explicit no-prose instruction.
        if re.search(r"(?:不写|不要写|先别写|暂不写|只(?:调整|修改|重排|规划|讨论))[^。！？\n]{0,28}(?:正文|章节|草稿|计划|安排)", text):
            return None

        if (re.search(r"(?:只|仅)修(?:订|正)?[^。！？\n]{0,24}草稿", text)
                and re.search(r"批次|这批", text)
                and _NO_ACCEPTANCE_PATTERN.search(text)):
            return TerminalIntent(
                action="batch_repair", requested_outcome=text[:1_000], confidence="high",
                authorization="approved", authorization_source="current_request",
                chapter_no=start_chapter, end_chapter_no=end_chapter,
                max_revision_rounds=2, operation_instruction=text[-4_000:],
                visible_reason=f"只修订现有批次第 {start_chapter}～{end_chapter} 章草稿并复审；不验收。",
            )

        asks_for_prose = re.search(r"(?<!已)(?:写|续写|创作|生成|完成|做完)[^。！？\n]{0,24}(?:正文|章节|章|草稿)", text)
        explicit_execution = re.search(r"(?:请|继续|接着|开始|直接|现在|马上|替我|帮我|给我)|(?:写完|做完|完成)", text)
        looks_like_question = re.search(r"(?:为什么|怎么|如何|是否|能不能|可不可以|行不行)[^。！？\n]*[？?]?$", text)
        if asks_for_prose is None or explicit_execution is None or looks_like_question is not None:
            return None

        acceptance_forbidden = _NO_ACCEPTANCE_PATTERN.search(text) is not None
        acceptance_requested = (
            not acceptance_forbidden
            and re.search(
                r"(?:自动|直接|通过后|没问题(?:的话)?|全部通过后)?"
                r"(?:验收|接收|接受|入正史|进入正史|收进正史|收进正文|收进去|定稿)",
                text,
            )
            is not None
        )
        action = "batch_draft_accept" if acceptance_requested else "batch_draft"
        return TerminalIntent(
            action=action,
            requested_outcome=text[:1_000],
            confidence="high",
            authorization="approved",
            authorization_source="batch_preapproval" if acceptance_requested else "current_request",
            acceptance_confirmation_mode=self.engine.settings.acceptance_confirmation_mode,
            chapter_no=start_chapter,
            end_chapter_no=end_chapter,
            max_revision_rounds=2,
            operation_instruction=text[-4_000:],
            visible_reason=(
                f"直接完成第 {start_chapter}～{end_chapter} 章；逐章处理硬问题，全部通过后整批验收。"
                if acceptance_requested
                else f"直接生成第 {start_chapter}～{end_chapter} 章临时草稿，不进入正史。"
            ),
        )

    @staticmethod
    def _deterministic_single_chapter_write(text: str) -> TerminalIntent | None:
        """Keep an explicit prose request from being reduced to a discussion.

        This only recognizes a single, directly named chapter. Planning,
        questions, negated writing, and ambiguous ranges still use Coordinator.
        A preceding fact check is part of the Writer/Editor job, not a reason
        to stop before writing.
        """

        if re.search(r"先聊|只讨论|怎么写|如何写|能不能写|可不可以写", text):
            return None
        if re.search(r"(?:不写|别写|不要写|先别写|暂不写|不能写|不会写)[^。！？\n]{0,20}第\s*\d+\s*章", text):
            return None
        if _CHAPTER_FROM_TO_PATTERN.search(text) or _CHAPTER_RANGE_PATTERN.search(text):
            return None
        matches = list(re.finditer(r"(?<![改重续])(?:续写|写|创作)(?:完|好)?第\s*(\d+)\s*章", text))
        if len(matches) != 1 or not re.search(r"请|接着|继续|开始|现在|直接|帮我|给我|写完|创作", text):
            return None
        chapter_no = int(matches[0].group(1))
        if chapter_no < 1:
            return None
        asks_review = re.search(r"审查|审核|检查|重审|审一遍|审一下|没过.*改|通过后", text) is not None
        accepts = not _NO_ACCEPTANCE_PATTERN.search(text) and re.search(
            r"(?:通过后|达标了?|没问题(?:的话)?|审核通过后|审查通过后)?(?:验收|接收|接受|入正史|进入正史|收进正史|收进正文|收进去|定稿)", text
        ) is not None
        # A local shortcut must not discard an unrecognised second intention.
        # Let Coordinator interpret compound requests instead of silently doing
        # only the first verb. Acceptance always includes necessary review.
        if not asks_review and not accepts and re.search(r"写好后|写完后|然后|之后|达标|合格|审|核对", text):
            return None
        action = "write_review_accept" if accepts else "write_review" if asks_review else "write_draft"
        return TerminalIntent(
            action=action, requested_outcome=text[:1_000], confidence="high",
            authorization="approved", authorization_source="current_request",
            chapter_no=chapter_no, narrative_scope="chapter", edit_scope="chapter",
            max_revision_rounds=2, operation_instruction=text[-4_000:],
            visible_reason=(
                f"写第 {chapter_no} 章，核对前文后审查并限次修订；通过才尝试入正史。" if accepts
                else f"写第 {chapter_no} 章，核对前文后审查并限次修订；本轮不自动入正史。" if asks_review
                else f"写第 {chapter_no} 章草稿；本轮不自动审查或入正史。"
            ),
        )

    @staticmethod
    def _confirmed_preference_intent(project: InkFlowProject, text: str) -> TerminalIntent | None:
        pending = TerminalSession._pending_question(project)
        cards = pending.get("questions", []) if pending else []
        if len(cards) != 1 or cards[0].get("id") != "preference-conflict":
            return None
        # Only an exact bound answer bypasses routing; additions need interpretation.
        matched = []
        for option in cards[0].get("options", []):
            label = option.get("label", "")
            expected = f"我来回答刚才的问题：\n1. {cards[0].get('question', '')}\n我的回答：{label}"
            full_answer = expected + "\n请结合这些答案继续理解原来的目标；如果此前已经明确要求执行且信息足够，就继续原任务，否则先总结你理解到的方案。"
            if option.get("kind") == "choice" and text.strip() in {label, expected, full_answer}:
                matched.append(option)
        if len(matched) != 1:
            return None
        saved = TerminalIntent.model_validate(pending["intent"])
        chosen_id = matched[0]["id"].removeprefix("preference-")
        items = project.db.effective_preferences() + preference_candidates(project.db)
        prior = pending.get("preference_resolutions", [])
        with use_preference_decisions(prior):
            apply_preference_decisions(items)  # Recheck previously chosen identities before continuing.
        resolved_pairs = {tuple(item["preference_ids"]) for item in prior if item["status"] == "resolved"}
        remaining = [item for item in saved.preference_conflicts if tuple(source.preference_id for source in item.sources) not in resolved_pairs]
        decisions = resolve_preference_conflicts(remaining, items, saved.user_message)
        if any(item["status"] == "stale" for item in decisions):
            raise ValidationGateError("所选习惯的原句或修订号已变化，请刷新取舍；未用旧选择继续原任务。")
        chosen = next((item for item in items if item["preference_id"] == chosen_id), None)
        if chosen is None:
            raise ValidationGateError("所选习惯已停用，不能据此继续原任务。")
        conflicts = [item.model_copy(update={"current_task_quote": text.strip()}) if chosen_id in
                     [source.preference_id for source in item.sources] else item for item in remaining]
        return saved.model_copy(update={"preference_conflicts": conflicts,
            "operation_instruction": saved.operation_instruction,
            "response_kind": "question_answer", "pending_question_id": pending["id"],
            "related_task_id": pending.get("task_id"), "user_message": text.strip()})

    @staticmethod
    def _confirmed_question_intent(text: str, pending_question: dict[str, Any] | None) -> TerminalIntent | None:
        """Reuse one fixed workflow choice; free-form answers still need routing."""
        if not pending_question or not pending_question.get("id"):
            return None
        cards = pending_question.get("questions") or []
        if len(cards) != 1 or not isinstance(cards[0], dict):
            return None
        card = cards[0]
        if card.get("selection") != "single":
            return None
        fixed = {"q1-run", "q1-discuss", "q1-primary", "q1-alternative"}
        options = [item for item in card.get("options", [])
                   if isinstance(item, dict) and item.get("id") in fixed]
        matched = [item for item in options if text.strip() == item.get("label")]
        if not matched:
            # Match the whole UI answer, so added instructions cannot be discarded.
            for item in options:
                expected = (
                    f"我来回答刚才的问题：\n1. {card.get('question', '')}\n"
                    f"我的回答：{item.get('label', '')}\n"
                    "请结合这些答案继续理解原来的目标；如果此前已经明确要求执行且信息足够，就继续原任务，否则先总结你理解到的方案。"
                )
                if text.strip() == expected:
                    matched.append(item)
        if len(matched) != 1:
            return None
        try:
            previous = TerminalIntent.model_validate(pending_question["intent"])
        except (KeyError, TypeError, ValueError):
            return None
        choice = matched[0]["id"]
        action = ("discuss" if choice == "q1-discuss" else
                  previous.alternative_action if choice == "q1-alternative" else previous.action)
        if not action or (action in {"discuss", "chat"} and choice != "q1-discuss"):
            return None
        return previous.model_copy(update={
            "action": action, "alternative_action": None, "confidence": "high",
            "authorization": "none" if choice == "q1-discuss" else "approved",
            "authorization_source": "none" if choice == "q1-discuss" else "current_request",
            "response_kind": "question_answer", "pending_question_id": pending_question["id"],
            "related_task_id": pending_question.get("task_id"),
            "clarification_question": "", "clarification_questions": [],
            "conversation_reply": "继续讨论原来的目标，本轮不执行。" if choice == "q1-discuss" else "",
        })

    @staticmethod
    def _confirmed_planning_intent(text: str, pending_question: dict[str, Any] | None) -> TerminalIntent | None:
        """Resume a confirmed outline request without dropping its target."""

        cards = pending_question.get("questions", []) if pending_question else []
        if (len(cards) == 1 and isinstance(cards[0], dict) and cards[0].get("id") == "planning-publish"
                and "我来回答刚才的问题：" in text
                and re.search(r"我的回答：确认正式采用这版规划(?:\s|$)", text)):
            return TerminalIntent(
                action="planning_publish_reviewed", requested_outcome=text[:1_000],
                confidence="high", authorization="approved", authorization_source="current_request",
                operation_instruction="确认采用刚才审核通过的规划",
                response_kind="question_answer", pending_question_id=pending_question["id"],
                related_task_id=pending_question.get("task_id"),
                visible_reason="用户通过规划提问卡确认正式采用已审核候选。",
            )
        if re.sub(r"[\s。！!，,]+", "", text) not in {"现在执行", "直接执行", "按刚才说的做", "就按刚才说的做"}:
            return None
        if not pending_question or not any(
            option.get("id") == "q1-run"
            for card in pending_question.get("questions", []) if isinstance(card, dict)
            for option in card.get("options", []) if isinstance(option, dict)
        ):
            return None
        try:
            previous = TerminalIntent.model_validate(pending_question["intent"])
        except (KeyError, TypeError, ValueError):
            return None
        if previous.document_kind not in {"outline", "story_detail"}:
            return None
        source = previous.requested_outcome or previous.operation_instruction
        matches = list(_CHAPTER_FROM_TO_PATTERN.finditer(source)) or list(_CHAPTER_RANGE_PATTERN.finditer(source))
        if previous.document_kind == "outline" and len(matches) != 1:
            return None
        start, end = (int(value) for value in matches[0].groups()) if matches else (None, None)
        if previous.document_kind == "outline" and (start is None or end is None or end < start):
            return None
        return previous.model_copy(update={
            "action": "outline", "outline_level": "detail" if previous.document_kind == "story_detail" else "story",
            "document_kind": "none", "setting_change": {},
            "chapter_no": start, "end_chapter_no": end,
            "authorization": "approved", "authorization_source": "current_request",
            "confidence": "high", "missing_fields": [], "clarification_question": "", "clarification_questions": [],
            "conversation_reply": "", "response_kind": "question_answer",
            "pending_question_id": pending_question["id"], "related_task_id": pending_question.get("task_id"),
            "visible_reason": "按刚才确认的范围修订大纲；保留原要求，不重新猜测目标或生成正文。",
        })

    @staticmethod
    def _apply_steering(original_message: str, steering_messages: list[str]) -> str:
        """Append confirmed user guidance only at a model-call boundary.

        Providers used by InkFlow do not accept new input in the middle of one
        request.  Re-routing at this boundary gives the Coordinator the same
        practical steering semantics without exposing private model reasoning or
        letting a late message bypass the Novel Engine's authorization gates.
        """
        guidance = "\n\n".join(f"- {item.strip()}" for item in steering_messages if item.strip())
        if not guidance:
            return original_message
        return (
            f"{original_message}\n\n"
            "# 用户的运行中引导\n"
            "以下内容是用户在本轮运行中确认的方向修正。它约束后续判断和动作；"
            "不得恢复已经不适用的未完成步骤，也不得因此绕过验收、正史或其他权限门禁。\n"
            f"{guidance}"
        )

    def _resolve_intent(
        self,
        project: InkFlowProject,
        message: str,
        intent: TerminalIntent,
    ) -> tuple[TerminalIntent, dict[str, Any] | None]:
        """Resolve only unique project references, then apply execution policy.

        The model may interpret broad natural language, but the host remains the
        authority for missing parameters, ambiguity and mutations of canon.
        """

        if (_requests_upper_plan_edit(message)
                and intent.action in {"plan", "outline", "plan_next_arc", "redesign_story"}
                and not re.search(r"先聊|讨论一下|能不能|会不会|不要执行", message)):
            boundary = project.db.latest_accepted_chapter_no()
            _, end = self._explicit_chapter_range(message)
            layers = _requested_upper_layers(message)
            single_layer = len(layers) == 1 and not re.search(r"三层|整套|近期(?:章节)?规划|章节规划|章节安排", message)
            active = load_active_planning(project) if single_layer else None
            intent = intent.model_copy(update={
                "action": "outline" if single_layer else "redesign_story",
                "outline_level": "detail" if layers == {"细纲"} else "story",
                "chapter_no": active[3].anchor_chapter + 1 if active else boundary,
                "end_chapter_no": active[3].chapters[-1].chapter_no if active else
                    end or intent.end_chapter_no or boundary + self.engine.settings.planning_window_chapters,
                "operation_instruction": message, "document_kind": "none", "setting_change": {},
                "missing_fields": [], "clarification_question": "", "clarification_questions": [],
                "visible_reason": "按原话仅修改指定规划层，下游先复核相容性；需要扩大修改范围再询问。" if single_layer else
                                  "先按正史修订大纲或卷细纲，再复核受影响的近期规划。",
            })

        # Route an explicitly requested complete hierarchy before interpreting
        # chapter numbers as legacy plan/outline parameters. Keep narrow requests.
        complete_planning = (
            re.search(
                r"(?:完整|整套)?三层规划|"
                r"大纲\s*(?:[、，,→—-]|再|然后|和|与|及)\s*(?:卷|各卷)?细纲"
                r"\s*(?:[、，,→—-]|再|然后|和|与|及|以及)\s*(?:近期(?:章节)?规划|章节规划)", message
            )
            and re.search(r"生成|制定|重设|重做|重构|设计|规划|整理", message)
            and not re.search(r"先聊|讨论|能不能|会不会|不要执行|先别|暂不|只(?:做|改|生成|整理)|(?:按|根据|参考|沿用|基于)(?:已有|现有|当前|生效)?(?:的)?(?:全书)?大纲", message)
        )
        if complete_planning and intent.action in {"plan", "outline", "plan_next_arc", "story_setting_edit", "redesign_story"}:
            boundary = project.db.latest_accepted_chapter_no()
            start, end = self._explicit_chapter_range(message)
            anchored_ranges = [match for pattern in (_CHAPTER_FROM_TO_PATTERN, _CHAPTER_RANGE_PATTERN)
                               for match in pattern.finditer(message)
                               if int(match.group(1)) in {boundary, boundary + 1}]
            if anchored_ranges:
                start, end = (int(value) for value in anchored_ranges[-1].groups())
            endpoint = re.search(r"(?:到|至)第?\s*(\d+)\s*章", message)
            if end is None and endpoint:
                start, end = None, int(endpoint.group(1))
            if start is not None and start not in {boundary, boundary + 1}:
                unresolved = intent.model_copy(update={
                    "action": "discuss", "authorization": "none", "authorization_source": "none",
                    "clarification_questions": [],
                    "clarification_question": f"当前正史到第 {boundary} 章。这次是从这里重设计完整三层规划，还是只调整你指定的章节范围？",
                })
                return unresolved, self._clarification_response(unresolved, fallback="完整三层规划与指定的局部范围需要区分。")
            intent = intent.model_copy(update={
                "action": "redesign_story", "chapter_no": boundary,
                "end_chapter_no": end or intent.end_chapter_no or boundary + self.engine.settings.planning_window_chapters,
                "operation_instruction": message, "document_kind": "none", "setting_change": {},
                "alternative_action": None if intent.alternative_action in {"plan", "outline", "plan_next_arc", "story_setting_edit", "redesign_story"} else intent.alternative_action,
                "missing_fields": [item for item in intent.missing_fields if item not in {"chapter_no", "end_chapter_no", "chapter_range", "document_kind", "setting_change"}],
                "visible_reason": "按完整三层规划流程生成并逐层审核；保留已接受正文，发布仍遵循当前确认设置。",
            })

        updates: dict[str, Any] = {}
        relevant_fields = self._relevant_missing_fields(intent.action)
        missing = {item for item in intent.missing_fields if item in relevant_fields}
        action = intent.action

        # A user who says “fix this chapter, then finish the batch and accept
        # it” is asking for the complete resumable workflow, not the narrower
        # repair-only tool.  Keep this deterministic so ordinary conversation
        # never exposes internal batch identifiers or strands the remaining
        # chapters behind a clarification dialog.
        if action == "batch_repair" and re.search(
            r"(?:通过|修好|改好)[^。；\n]{0,32}(?:继续|接着|写完)[^。；\n]{0,32}(?:验收|正史|收进)",
            message,
        ):
            action = "batch_draft_accept"
            updates["action"] = action
            missing.discard("batch_id")

        if action == "settings_update":
            patch = self._sanitize_settings_patch(intent.settings_patch or self._parse_settings_patch(message))
            updates["settings_patch"] = patch
            if not patch:
                missing.add("settings_patch")
            else:
                missing.discard("settings_patch")

        explicit_start, explicit_end = self._explicit_chapter_range(message)
        endpoints = {int(item.group(1)) for item in _CHAPTER_END_PATTERN.finditer(message)}
        writing_actions = {"batch_draft", "batch_draft_accept", "batch_repair", "continue_run",
                           "write_draft", "write_review", "write_review_accept", "revise_draft", "revise_review", "revise_review_accept"}
        if action in writing_actions and (len(endpoints) > 1 or (explicit_end is not None and endpoints and endpoints != {explicit_end})):
            targets = sorted(endpoints | ({explicit_end} if explicit_end is not None else set()))
            question = "这次要写到哪一章停下：" + "还是".join(f"第 {number} 章" for number in targets) + "？旧任务会保留，确认前不继续它。"
            unresolved = intent.model_copy(update={"clarification_question": question, "clarification_questions": [], "batch_id": None})
            return unresolved, self._clarification_response(unresolved, fallback="当前输入给出了不同终点。")
        if action in writing_actions and endpoints and explicit_end is None:
            explicit_start = project.db.latest_accepted_chapter_no() + 1
            explicit_end = next(iter(endpoints))
            if explicit_end < explicit_start:
                unresolved = intent.model_copy(update={"clarification_question": f"第 {explicit_end} 章已在正史范围内。这次是修改已接受正文，还是从第 {explicit_start} 章继续？", "clarification_questions": [], "batch_id": None})
                return unresolved, self._clarification_response(unresolved, fallback="这个终点不属于尚未接受的续写范围。")
            action = {"write_draft": "batch_draft", "write_review": "batch_draft", "revise_draft": "batch_draft",
                      "revise_review": "batch_draft", "write_review_accept": "batch_draft_accept",
                      "revise_review_accept": "batch_draft_accept"}.get(action, action)
            updates.update(action=action, batch_id=None,
                           visible_reason=f"按本次终点从第 {explicit_start} 章推进到第 {explicit_end} 章后停止；旧范围任务保留，不自动沿用。")
            missing.discard("batch_id")
        if (action == "batch_repair" and explicit_end is not None
                and re.search(r"(?:继续|接着)[^。；\n]{0,16}(?:写|续写|创作)|写到|做到|停在第\s*\d+\s*章", message)):
            pending_task = project.db.get_metadata(conversation_key("pending_creation_task"))
            saved_action = (pending_task.get("intent", {}).get("action")
                            if isinstance(pending_task, dict) else None)
            accepts = (saved_action == "batch_draft_accept"
                       or re.search(r"验收|接收|接受|正史|收进正文", message) is not None)
            action = "batch_draft_accept" if accepts and not _NO_ACCEPTANCE_PATTERN.search(message) else "batch_draft"
            updates.update(action=action, batch_id=None)
            missing.discard("batch_id")
        if action in _CHAPTER_ACTIONS and explicit_start is not None:
            updates["chapter_no"] = explicit_start
            missing.discard("chapter_no")
        requested_chapter = updates.get("chapter_no", intent.chapter_no)
        if action in {"revise_draft", "revise_review", "revise_review_accept"} and requested_chapter:
            existing = project.db.get_chapter(int(requested_chapter))
            if existing and existing["status"] == "accepted":
                if _NO_ACCEPTANCE_PATTERN.search(message):
                    action = "discuss"
                    updates.update(action=action, authorization="none", narrative_scope="none",
                                   conversation_reply="这章已经是正文。我可以先说明疑点和可修的范围；你若要我实际修复，请直接说‘自己核对并修好这一章’。")
                else:
                    action = "repair_accepted"
                    updates.update(action=action, narrative_scope="chapter", edit_scope="selection",
                                   visible_reason="先核对已接受正文的两处证据；仅在可安全补足时局部修订并保留旧版。")
                missing.discard("chapter_no")
        if action in {"plan", "plan_preview", "outline", "arc_audit", "batch_draft", "batch_draft_accept", "batch_repair"} and explicit_start is not None:
            updates["chapter_no"] = explicit_start
            updates["end_chapter_no"] = explicit_end or explicit_start
            if action in {"batch_draft", "batch_draft_accept"} and explicit_end is not None:
                updates["batch_id"] = None
            if action != "plan":
                missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})
            elif explicit_end is None:
                updates.pop("chapter_no", None)
                updates.pop("end_chapter_no", None)
        if (action in {"batch_draft", "batch_draft_accept"}
                and re.search(r"(?:这批|当前批次|继续|接着|断点)", message)
                and not _explicitly_requests_fresh_batch(message)):
            resumable = self._resumable_batch_ids(project)
            requested_end = updates.get("end_chapter_no", intent.end_chapter_no)
            if requested_end is not None:
                resumable = [batch_id for batch_id in resumable
                             if (span := self._batch_chapter_range(project, batch_id)) and span[1] == requested_end]
            if explicit_start is not None:
                resumable = [
                    batch_id for batch_id in resumable
                    if (span := self._batch_chapter_range(project, batch_id))
                    and span[0] <= explicit_start <= (explicit_end or explicit_start) <= span[1]
                    and (explicit_end is None or explicit_end == span[1])
                ]
            if len(resumable) == 1:
                span = self._batch_chapter_range(project, resumable[0])
                if span is not None:
                    updates["chapter_no"], updates["end_chapter_no"] = span
                    updates["batch_id"] = resumable[0]
                    missing.difference_update({"batch_id", "chapter_no", "end_chapter_no", "chapter_range"})
        if action == "continue_run" and explicit_start is not None:
            updates["end_chapter_no"] = explicit_end or explicit_start
            missing.difference_update({"end_chapter_no", "target"})
        if action in {"rollback_preview", "rollback_restore"} and not intent.checkpoint_id:
            before_match = _BEFORE_CHAPTER_PATTERN.search(message)
            if before_match and int(before_match.group(1)) > 1:
                updates["chapter_no"] = int(before_match.group(1)) - 1
                missing.discard("checkpoint_id")

        if action in _CHAPTER_ACTIONS and intent.chapter_no is None and "chapter_no" not in updates:
            chapter_no = self._unique_chapter_reference(project, action)
            if chapter_no is not None:
                updates["chapter_no"] = chapter_no
                missing.discard("chapter_no")

        if (action in {"plan_preview", "arc_audit"} or (action == "outline" and intent.outline_level == "story")) and (
            intent.chapter_no is None or intent.end_chapter_no is None
        ):
            bundle = project.db.get_current_plan_bundle()
            if bundle is not None:
                whole_book = action == "outline" and intent.outline_level == "story"
                updates.setdefault("chapter_no", 1 if whole_book else bundle.current_arc.chapter_start)
                updates.setdefault("end_chapter_no", bundle.book.estimated_chapters if whole_book else bundle.current_arc.chapter_end)
                missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})
            elif action == "outline" and intent.outline_level == "story":
                # A new book has a brief before it has executable chapter cards.
                brief = project.db.get_brief()
                updates.setdefault("chapter_no", intent.chapter_no or 1)
                updates.setdefault("end_chapter_no", intent.end_chapter_no or brief.estimated_chapters)
                missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})

        if action == "batch_accept" and not intent.batch_id:
            ready_batches = self._ready_batch_ids(project)
            if len(ready_batches) == 1:
                updates["batch_id"] = ready_batches[0]
                missing.discard("batch_id")

        if action == "batch_repair":
            if intent.batch_id and intent.batch_id not in message:
                # Model-supplied identifiers are not user authority.
                intent = intent.model_copy(update={"batch_id": None})
            requested_start = updates.get("chapter_no", intent.chapter_no)
            requested_end = updates.get("end_chapter_no", intent.end_chapter_no)
            if not intent.batch_id:
                repairable_batches = self._repairable_batch_ids(project)
                pending_task = project.db.get_metadata(conversation_key("pending_creation_task"))
                pending_batch = (pending_task.get("intent", {}).get("batch_id")
                                 if isinstance(pending_task, dict) else None)
                if re.search(r"当前|刚才|这批|临时批次", message) and pending_batch in repairable_batches:
                    span = self._batch_chapter_range(project, pending_batch)
                    if span and (requested_start is None or span[0] <= requested_start <= span[1]) and (requested_end is None or requested_end == span[1]):
                        repairable_batches = [pending_batch]
                if requested_start is not None and requested_end is not None:
                    repairable_batches = [
                        batch_id for batch_id in repairable_batches
                        if (span := self._batch_chapter_range(project, batch_id))
                        and span[0] <= requested_start <= requested_end <= span[1]
                        and requested_end == span[1]
                    ]
                if len(repairable_batches) == 1:
                    updates["batch_id"] = repairable_batches[0]
                    missing.discard("batch_id")
            candidate_batch = str(updates.get("batch_id") or intent.batch_id or "")
            if candidate_batch and (intent.chapter_no is None or intent.end_chapter_no is None):
                batch_range = self._batch_chapter_range(project, candidate_batch)
                if batch_range is not None:
                    updates.setdefault("chapter_no", batch_range[0])
                    updates.setdefault("end_chapter_no", batch_range[1])
                    missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})

        if action == "outline" and intent.outline_level == "detail":
            updates.update(chapter_no=None, end_chapter_no=None)
            missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})
        explicit_batch = re.search(r"\bbatch-[0-9a-f]{32}\b", message)
        if explicit_batch and action in {"batch_draft", "batch_draft_accept", "batch_repair", "batch_accept"}:
            updates["batch_id"] = explicit_batch.group()
            missing.discard("batch_id")
        resolved = intent.model_copy(update={**updates, "missing_fields": sorted(missing)})
        required_missing = self._required_missing(resolved)
        if required_missing:
            resolved = resolved.model_copy(
                update={"missing_fields": sorted(set(resolved.missing_fields) | required_missing)}
            )

        if (
            resolved.action == "plan_next_arc"
            and resolved.authorization == "approved"
            and resolved.operation_instruction.strip()
        ):
            resolved = resolved.model_copy(update={"plan_change_confirmed": True})

        if resolved.action in {"discuss", "chat"}:
            # 闲聊与讨论都不要求执行授权，直接由墨宝口吻回复。
            return resolved, None

        if resolved.missing_fields:
            return resolved, self._clarification_response(
                resolved,
                fallback="我已经理解大致目标，但还缺少执行所需的信息。",
            )

        inquiry_frequency = self.engine.settings.inquiry_frequency
        if resolved.confidence == "low" and inquiry_frequency != "low":
            return resolved, self._clarification_response(
                resolved,
                fallback="这句话有两种以上合理理解，我不想替你猜错。",
            )

        if resolved.alternative_action:
            return resolved, self._clarification_response(
                resolved,
                fallback="我能想到两种都合理的处理方式，需要你确认其中一种。",
            )

        should_optional_ask = (
            resolved.action not in _READ_ONLY_ACTIONS
            and bool(resolved.clarification_question.strip())
            and not _NO_OPTIONAL_QUESTION_PATTERN.search(message)
            and (
                inquiry_frequency == "ultra"
                or (inquiry_frequency == "high" and resolved.confidence == "medium")
            )
        )
        if should_optional_ask:
            return resolved, self._clarification_response(
                resolved,
                fallback="为了让这次创作更贴近你的想法，我先确认一个会明显影响结果的选择。",
            )

        if resolved.action not in _READ_ONLY_ACTIONS and resolved.authorization != "approved":
            risk_note = (
                "这一步会进入或改变正史/未来规划，需要你明确表示现在执行。"
                if resolved.action in _CANON_MUTATION_ACTIONS
                else "我把这句话理解为讨论或建议，还没有直接开始执行。"
            )
            if resolved.action in _RESTORE_ACTIONS:
                risk_note = "回退会改变当前项目状态，必须先预览影响并明确确认。"
            return resolved, self._clarification_response(resolved, fallback=risk_note)

        return resolved, None

    def _apply_acceptance_policy(self, message: str, intent: TerminalIntent) -> TerminalIntent:
        """把用户保存的自动验收授权应用到含 Reviewer 的固定工作流。"""

        mode = self.engine.settings.acceptance_confirmation_mode
        intent = intent.model_copy(update={"acceptance_confirmation_mode": mode})
        # A draft-only request is a hard per-request constraint.  It must win
        # over the saved batch/auto acceptance preference, including fuzzy
        # natural-language wording such as “先给我看成品，暂时别收进正史”。
        if _NO_ACCEPTANCE_PATTERN.search(message) or "accept" in intent.forbidden_actions:
            downgrade = {
                "batch_draft_accept": "batch_draft",
                "write_review_accept": "write_review",
                "revise_review_accept": "revise_review",
                "review_accept": "review",
                "accept": "review",
                "batch_accept": "discuss",
                "continue_run": "discuss",
            }.get(intent.action)
            return intent.model_copy(
                update={
                    "action": downgrade or intent.action,
                    "authorization_source": "current_request",
                    "conversation_reply": "这次先不进入正史；可以先生成或查看草稿。" if downgrade == "discuss" else intent.conversation_reply,
                }
            )
        if mode == "batch_once" and intent.action == "batch_draft" and intent.authorization == "approved":
            return intent.model_copy(
                update={
                    "action": "batch_draft_accept",
                    "authorization_source": "batch_preapproval",
                    "visible_reason": "按已保存的批次验收授权推进草稿、审查和必要修订；整批通过审查后才集中接收并进入正史。",
                }
            )
        if mode != "auto_after_review":
            return intent
        upgraded = {
            "review": "review_accept",
            "write_review": "write_review_accept",
            "revise_review": "revise_review_accept",
            "batch_draft": "batch_draft_accept",
        }.get(intent.action)
        if not upgraded or intent.authorization != "approved":
            return intent
        reasons = {
            "review_accept": "按已保存的自动验收设置审查当前章节；只有审查通过后才接收并进入正史。",
            "write_review_accept": "按已保存的自动验收设置先写草稿再审查；只有审查通过后才接收并进入正史。",
            "revise_review_accept": "按已保存的自动验收设置先修订再审查；只有修订后的当前版本通过审查才接收并进入正史。",
            "batch_draft_accept": "按已保存的自动验收设置推进批次草稿、审查和必要修订；整批通过审查后才集中接收并进入正史。",
        }
        return intent.model_copy(
            update={"action": upgraded, "authorization_source": "settings_auto_accept", "visible_reason": reasons[upgraded]}
        )

    @classmethod
    def _coerce_settings_intent(cls, message: str, intent: TerminalIntent) -> TerminalIntent:
        """Turn a clear settings request into a bounded Coordinator route.

        The language can be loose, but only values understood by the host are
        accepted.  Questions such as “怎么调” stay in discussion mode.
        """

        patch = cls._sanitize_settings_patch(cls._parse_settings_patch(message))
        setting_words = re.search(r"(?:设置|参数|温度|top[\s_-]*p|上下文|容错|把握度|置信度|自动朗读|语音输出|语音输入|模型|思考|推理)", message, re.I)
        imperative = re.search(r"(?:设置|调整|调到|调成|调为|调低|调高|降低|提高|改成|改为|换成|开启|关闭|设为|默认)", message)
        if patch and setting_words and imperative and intent.action in {"chat", "discuss", "settings_update"}:
            return intent.model_copy(
                update={
                    "action": "settings_update",
                    "settings_patch": patch,
                    "authorization": "approved",
                    "authorization_source": "current_request",
                    "requested_outcome": "按用户当前说法调整墨流设置",
                    "visible_reason": "Coordinator 已识别到明确设置变更，并会在完成后列出每一项变化。",
                    "confidence": "high",
                }
            )
        return intent

    @staticmethod
    def _sanitize_settings_patch(patch: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {}
        allowed = {
            "model", "reasoning_effort", "inquiry_frequency", "hook_strategy",
            "chapter_length_tolerance", "review_min_confidence", "acceptance_confirmation_mode",
            "planning_publication_mode",
            "planning_window_chapters",
            "context_budget_mode", "context_soft_tokens", "context_hard_tokens",
            "voice_auto_read", "voice_output_enabled", "voice_input_enabled",
            "agent_generation",
        }
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in allowed:
                continue
            try:
                if key in {"chapter_length_tolerance", "review_min_confidence"}:
                    number = float(value)
                    if not math.isfinite(number):
                        continue
                    if number > 1:
                        number /= 100
                    lower, upper = (0.05, 0.30) if key == "chapter_length_tolerance" else (0.70, 1.0)
                    if lower <= number <= upper:
                        clean[key] = round(number, 4)
                elif key in {"context_soft_tokens", "context_hard_tokens"}:
                    number = int(value)
                    if 16_000 <= number <= 2_000_000:
                        clean[key] = number
                elif key == "planning_window_chapters":
                    number = int(value)
                    if 1 <= number <= 50:
                        clean[key] = number
                elif key in {"voice_auto_read", "voice_output_enabled", "voice_input_enabled"}:
                    if isinstance(value, str):
                        normalized = value.strip().casefold()
                        if normalized in {"1", "true", "yes", "on", "open", "开启", "打开", "是"}:
                            clean[key] = True
                        elif normalized in {"0", "false", "no", "off", "close", "关闭", "取消", "否"}:
                            clean[key] = False
                    elif isinstance(value, (bool, int, float)):
                        clean[key] = bool(value)
                elif key == "reasoning_effort" and str(value).casefold() in {"low", "medium", "high", "max"}:
                    clean[key] = str(value).casefold()
                elif key == "inquiry_frequency" and str(value).casefold() in {"low", "medium", "high", "ultra"}:
                    clean[key] = str(value).casefold()
                elif key == "hook_strategy" and str(value) in {"most_chapters", "key_chapters", "natural_afterglow"}:
                    clean[key] = str(value)
                elif key == "acceptance_confirmation_mode" and str(value) in {"per_chapter", "batch_once", "auto_after_review"}:
                    clean[key] = str(value)
                elif key == "planning_publication_mode" and str(value) in {"auto_after_review", "confirm_after_review"}:
                    clean[key] = str(value)
                elif key == "context_budget_mode" and str(value) in {"unified", "custom"}:
                    clean[key] = str(value)
                elif key == "agent_generation" and isinstance(value, dict):
                    generation: dict[str, dict[str, float | int | None]] = {}
                    for role, raw in value.items():
                        if role not in AGENT_ROLES or not isinstance(raw, dict):
                            continue
                        role_values: dict[str, float | int | None] = {}
                        if "temperature" in raw:
                            temperature = float(raw["temperature"])
                            if math.isfinite(temperature) and 0 <= temperature <= 2:
                                role_values["temperature"] = temperature
                        if "top_p" in raw:
                            top_p = float(raw["top_p"])
                            if math.isfinite(top_p) and 0 < top_p <= 1:
                                role_values["top_p"] = top_p
                        if "top_k" in raw and raw["top_k"] not in (None, "", 0, "0"):
                            top_k = int(raw["top_k"])
                            if 1 <= top_k <= 200:
                                role_values["top_k"] = top_k
                        if role_values:
                            generation[role] = role_values
                    if generation:
                        clean[key] = generation
                elif key == "model" and str(value).strip() and len(str(value).strip()) <= 160:
                    clean[key] = str(value).strip()
            except (TypeError, ValueError, OverflowError):
                # One malformed field must not discard otherwise valid changes.
                continue
        return clean

    @staticmethod
    def _parse_settings_patch(message: str) -> dict[str, Any]:
        compact = re.sub(r"\s+", "", message.lower())
        patch: dict[str, Any] = {}
        tolerance = re.search(r"(?:字数|章节)?(?:容错|容差|误差)(?:率)?(?:改为|设为|调整为|控制在)?(\d{1,3})%", compact)
        if tolerance:
            patch["chapter_length_tolerance"] = int(tolerance.group(1)) / 100
        confidence = re.search(r"(?:最低)?(?:把握度|置信度)(?:改为|设为|至少|不低于)?(\d{1,3})%", compact)
        if confidence:
            patch["review_min_confidence"] = int(confidence.group(1)) / 100
        if re.search(r"(?:关闭|取消|不要)自动(?:验收|接收)", compact):
            patch["acceptance_confirmation_mode"] = "per_chapter"
        elif re.search(r"(?:不用|不要|不必|无需)(?:再)?(?:逐章|每章|一个个)确认|自动(?:验收|接收)|通过.*自动", compact):
            patch["acceptance_confirmation_mode"] = "auto_after_review"
        elif re.search(r"(?:逐章|每章).*确认|每个章节.*确认", compact):
            patch["acceptance_confirmation_mode"] = "per_chapter"
        elif re.search(r"(?:批量|批次).*确认|确认一次", compact):
            patch["acceptance_confirmation_mode"] = "batch_once"
        elif re.search(r"自动(?:验收|接收)|通过.*自动", compact):
            patch["acceptance_confirmation_mode"] = "auto_after_review"
        if re.search(r"(?:少问|少打断|不要总问)", compact):
            patch["inquiry_frequency"] = "low"
        elif re.search(r"(?:多问|经常确认|主动问)", compact):
            patch["inquiry_frequency"] = "high"
        if re.search(r"各个模型.*独立|自定义.*上下文|分别调整", compact):
            patch["context_budget_mode"] = "custom"
        elif re.search(r"统一.*上下文|综合.*上下文", compact):
            patch["context_budget_mode"] = "unified"
        if re.search(r"关闭.*自动朗读|不要自动播报|取消自动朗读", compact):
            patch["voice_auto_read"] = False
        elif re.search(r"开启.*自动朗读|打开.*自动播报|自动朗读", compact):
            patch["voice_auto_read"] = True
        if re.search(r"关闭.*语音输出|不要.*语音输出", compact):
            patch["voice_output_enabled"] = False
        elif re.search(r"开启.*语音输出|打开.*语音输出", compact):
            patch["voice_output_enabled"] = True
        if re.search(r"关闭.*语音输入|不要.*语音输入", compact):
            patch["voice_input_enabled"] = False
        elif re.search(r"开启.*语音输入|打开.*语音输入", compact):
            patch["voice_input_enabled"] = True
        effort = re.search(r"(?:思考|推理)(?:强度)?(?:改为|设为|调到|调成|调整为|调|设|改)?(低|中|高|最高|max|low|medium|high)", compact)
        if effort:
            patch["reasoning_effort"] = {"低": "low", "中": "medium", "高": "high", "最高": "max"}.get(effort.group(1), effort.group(1))
        temperature = re.search(r"(?:(writer|写作|coordinator|协调|memory[_-]?keeper|记忆整理|记忆|reviewer|专项审查|editor|编辑|审查)[^\d]{0,8})?(?:temperature|温度)[^\d]{0,8}(0(?:\.\d+)?|1(?:\.\d+)?|2(?:\.0)?)", compact)
        top_p = re.search(r"(?:(writer|写作|coordinator|协调|memory[_-]?keeper|记忆整理|记忆|reviewer|专项审查|editor|编辑|审查)[^\d]{0,8})?(?:top[-_ ]?p|topp)[^\d]{0,8}(0?\.\d+|1(?:\.0)?)", compact)
        if temperature or top_p:
            role = "writer"
            role_hint = (temperature.group(1) if temperature else None) or (top_p.group(1) if top_p else None) or ""
            if re.search(r"coordinator|协调", role_hint):
                role = "coordinator"
            elif re.search(r"memory[_-]?keeper|记忆", role_hint):
                role = "memory_keeper"
            elif re.search(r"reviewer|专项审查", role_hint):
                role = "reviewer"
            elif re.search(r"editor|编辑|审查", role_hint):
                role = "editor"
            generation: dict[str, Any] = {role: {}}
            if temperature:
                generation[role]["temperature"] = float(temperature.group(2))
            if top_p:
                generation[role]["top_p"] = float(top_p.group(2))
            patch["agent_generation"] = generation
        model = re.search(r"(?:模型|默认模型)(?:改为|设为|换成|使用)([a-z0-9_.:/-]{2,160})", compact)
        if model:
            patch["model"] = model.group(1)
        if re.search(r"规划|大纲|细纲", compact) and re.search(r"审核|发布|采用|生效", compact):
            patch.pop("acceptance_confirmation_mode", None)
            if re.search(r"等我确认|由我确认|人工确认|先确认|确认后", compact):
                patch["planning_publication_mode"] = "confirm_after_review"
            elif re.search(r"自动发布|自动采用|自动生效|审核通过就发布", compact):
                patch["planning_publication_mode"] = "auto_after_review"
        return patch

    @staticmethod
    def _explicit_chapter_range(message: str) -> tuple[int | None, int | None]:
        # A natural request can quote an audit scope before naming its real
        # target, e.g. "根据第 1 到 10 章复审，修第 9 到第 10 章".  The
        # final explicit range is normally the operative object, while the
        # earlier one is supporting evidence.
        explicit_spans = list(_CHAPTER_FROM_TO_PATTERN.finditer(message))
        if explicit_spans:
            span = explicit_spans[-1]
            start = int(span.group(1))
            end = int(span.group(2))
            if start >= 1 and end >= start:
                return start, end
        range_matches = list(_CHAPTER_RANGE_PATTERN.finditer(message))
        if range_matches:
            range_match = range_matches[-1]
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start >= 1 and end >= start:
                return start, end
        if re.search(r"(?:继续|接着|往下|一路|一直)[^。！？；\n]{0,16}(?:写|续写|创作|完成)(?:到|至)第?\s*\d+\s*章", message):
            # A lone number is the destination, not a request to start there.
            return None, None
        number_match = _CHAPTER_NUMBER_PATTERN.search(message)
        if number_match:
            chapter_no = int(number_match.group(1))
            if chapter_no >= 1:
                return chapter_no, None
        return None, None

    def _unique_chapter_reference(self, project: InkFlowProject, action: str) -> int | None:
        drafts = project.db.chapter_numbers_by_status("draft")
        if action == "repair_accepted":
            hold = project.latest_accepted_quality_hold()
            return int(hold["chapter_no"]) if hold else None
        if action in {"review", "revise_draft", "revise_review", "review_accept", "revise_review_accept", "accept"}:
            return drafts[0] if len(drafts) == 1 else None
        if action in {"write_draft", "write_review", "write_review_accept"}:
            next_chapter = project.db.latest_accepted_chapter_no() + 1
            return next_chapter
        return None

    @staticmethod
    def _ready_batch_ids(project: InkFlowProject) -> list[str]:
        folder = project.internal / "batches"
        result: list[str] = []
        for path in folder.glob("batch-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = value.get("status")
            start = int(value.get("start_chapter_no") or 0)
            end = int(value.get("end_chapter_no") or 0)
            next_chapter = project.db.latest_accepted_chapter_no() + 1
            if (status in {"ready_for_acceptance", "accepting"}
                    and start <= next_chapter <= end + 1
                    and value.get("batch_id") == path.stem):
                result.append(path.stem)
        return sorted(result)

    @staticmethod
    def _repairable_batch_ids(project: InkFlowProject) -> list[str]:
        folder = project.internal / "batches"
        result: list[str] = []
        for path in folder.glob("batch-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            repairable_status = value.get("status") in {"ready_for_acceptance", "needs_revision", "interrupted"}
            if repairable_status and value.get("batch_id") == path.stem:
                result.append(path.stem)
        return sorted(result)

    @staticmethod
    def _resumable_batch_ids(project: InkFlowProject) -> list[str]:
        """Return only unfinished batches attached to the next canon chapter."""

        expected_start = project.db.latest_accepted_chapter_no() + 1
        folder = project.internal / "batches"
        result: list[str] = []
        for path in folder.glob("batch-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                start = int(value.get("start_chapter_no") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                start == expected_start
                and value.get("status") in {"interrupted", "failed", "needs_revision", "ready_for_acceptance"}
                and value.get("batch_id") == path.stem
            ):
                result.append(path.stem)
        return sorted(result)

    @staticmethod
    def _batch_chapter_range(project: InkFlowProject, batch_id: str) -> tuple[int, int] | None:
        path = project.internal / "batches" / f"{batch_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            start = int(value["start_chapter_no"])
            end = int(value["end_chapter_no"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return (start, end) if start >= 1 and end >= start else None

    @staticmethod
    def _required_missing(intent: TerminalIntent) -> set[str]:
        missing: set[str] = set()
        if intent.action in _CHAPTER_ACTIONS and intent.chapter_no is None:
            missing.add("chapter_no")
        if intent.action in {"plan_preview", "arc_audit", "batch_draft", "batch_draft_accept", "batch_repair"} or (intent.action == "outline" and intent.outline_level == "story"):
            if intent.chapter_no is None or intent.end_chapter_no is None:
                missing.add("chapter_range")
        if intent.action in {"batch_accept", "batch_repair"} and not intent.batch_id:
            missing.add("batch_id")
        if intent.action == "continue_run":
            if intent.target_characters is None and intent.end_chapter_no is None:
                missing.add("target")
        if intent.action == "settings_update" and not intent.settings_patch:
            missing.add("settings_patch")
        if intent.action in {"rollback_preview", "rollback_restore"}:
            if not intent.checkpoint_id and intent.chapter_no is None:
                missing.add("checkpoint_id")
        if intent.action == "rollback_restore" and not intent.confirmation_token:
            missing.add("confirmation_token")
        if intent.action == "planning_history_restore" and intent.planning_revision_no is None:
            missing.add("planning_revision_no")
        if intent.action == "planning_history_view" and intent.planning_revision_no is not None and intent.planning_part == "none":
            missing.add("planning_part")
        return missing

    @staticmethod
    def _relevant_missing_fields(action: str) -> set[str]:
        fields: set[str] = set()
        if action in _CHAPTER_ACTIONS:
            fields.add("chapter_no")
        if action in {"plan_preview", "outline", "arc_audit", "batch_draft", "batch_draft_accept", "batch_repair"}:
            fields.update({"chapter_no", "end_chapter_no", "chapter_range"})
        if action in {"batch_accept", "batch_repair"}:
            fields.add("batch_id")
        if action == "continue_run":
            fields.update({"end_chapter_no", "target_characters", "target"})
        if action == "settings_update":
            fields.add("settings_patch")
        if action in {"rollback_preview", "rollback_restore"}:
            fields.update({"checkpoint_id", "chapter_no"})
        if action == "rollback_restore":
            fields.add("confirmation_token")
        if action == "planning_history_restore":
            fields.add("planning_revision_no")
        if action == "planning_history_view":
            fields.update({"planning_revision_no", "planning_part"})
        return fields

    @staticmethod
    def _clarification_response(intent: TerminalIntent, *, fallback: str) -> dict[str, Any]:
        labels = [_FIELD_LABELS.get(item, item) for item in intent.missing_fields]
        question = intent.clarification_question.strip()
        if not question and labels:
            question = f"请补充{'、'.join(labels)}，其余要求不用重说。"
        if not question:
            outcome = intent.requested_outcome.strip() or intent.visible_reason
            question = f"我理解你想要“{outcome}”。你是要我现在执行，还是先继续讨论？"
        return {
            "reply": f"{fallback} {question}".strip(),
            "needs_clarification": True,
            "questions": TerminalSession._question_cards(intent, fallback_question=question),
            "routing": {
                "candidate_action": intent.action,
                "alternative_action": intent.alternative_action,
                "missing_fields": labels,
                "confidence": intent.confidence,
                "authorization": intent.authorization,
            },
        }

    @staticmethod
    def _question_cards(intent: TerminalIntent, *, fallback_question: str = "") -> list[dict[str, Any]]:
        """Normalize model questions and guarantee a final free-text choice."""

        cards: list[dict[str, Any]] = []
        for index, question in enumerate(intent.clarification_questions[:3], start=1):
            options = [
                {
                    "id": f"q{index}-o{option_index}",
                    "label": option.label.strip(),
                    "description": option.description.strip(),
                    "recommended": option.recommended,
                    "kind": "choice",
                }
                for option_index, option in enumerate(question.options[:4], start=1)
                if option.label.strip() and "其他" not in option.label
            ]
            options.append(
                {
                    "id": f"q{index}-other",
                    "label": "其他",
                    "description": "用自己的话填写；不必套用上面的选项。",
                    "recommended": False,
                    "kind": "other",
                }
            )
            cards.append(
                {
                    "id": f"question-{index}",
                    "header": question.header.strip() or "需要确认",
                    "question": question.question.strip(),
                    "why_it_matters": question.why_it_matters.strip(),
                    "selection": question.selection,
                    "options": options,
                }
            )
        if cards:
            return cards

        question = fallback_question or intent.clarification_question.strip()
        if not question:
            outcome = intent.requested_outcome.strip() or intent.visible_reason
            question = f"我理解你想要“{outcome}”。你是要我现在执行，还是先继续讨论？"
        options: list[dict[str, Any]] = []
        if intent.alternative_action:
            options.extend(
                [
                    {
                        "id": "q1-primary",
                        "label": "按主要理解处理",
                        "description": f"采用当前识别到的 {intent.action} 路径。",
                        "recommended": intent.confidence != "low",
                        "kind": "choice",
                    },
                    {
                        "id": "q1-alternative",
                        "label": "采用另一种理解",
                        "description": f"改用 {intent.alternative_action} 路径。",
                        "recommended": False,
                        "kind": "choice",
                    },
                ]
            )
        elif not intent.missing_fields and re.search(
            r"现在执行|直接执行|开始执行|继续讨论|要我.{0,8}执行|是否.{0,8}执行", question
        ):
            options.extend(
                [
                    {
                        "id": "q1-run",
                        "label": "现在执行",
                        "description": "按刚才已经说明的目标开始，不再追加可选偏好。",
                        "recommended": intent.confidence == "high",
                        "kind": "choice",
                    },
                    {
                        "id": "q1-discuss",
                        "label": "先继续讨论",
                        "description": "先澄清方向，本轮不修改正文、规划或正史。",
                        "recommended": intent.confidence != "high",
                        "kind": "choice",
                    },
                ]
            )
        options.append(
            {
                "id": "q1-other",
                "label": "其他",
                "description": "补充章节号、范围、偏好，或用自己的话回答。",
                "recommended": False,
                "kind": "other",
            }
        )
        return [
            {
                "id": "question-1",
                "header": "需要确认",
                "question": question,
                "why_it_matters": "你的回答会决定下一步工作范围；未回答前不会执行这次任务。",
                "selection": "single",
                "options": options,
            }
        ]

    @staticmethod
    def _work_source(project: InkFlowProject) -> dict[str, Any]:
        from .story_settings import StorySettingsService
        from .preferences import preference_prompt
        drafts = [project.db.get_chapter(number) for number in project.db.chapter_numbers_by_status("draft")]
        return {"files": {name: content_hash((project.root / name).read_text(encoding="utf-8"))
                          if (project.root / name).is_file() else None
                          for name in ("BOOK.md", "STATE.md", "planning/active-v2.json")},
                "chapters": [[row["chapter_no"], row["version"], row["content_hash"]]
                             for row in project.db.accepted_chapters()],
                "drafts": [[row["chapter_no"], row["version"], row["content_hash"]] for row in drafts if row],
                "settings": StorySettingsService(project).source_fingerprint(),
                "preferences": content_hash(preference_prompt(project.db))}

    def _build_packet(self, project: InkFlowProject, message: str) -> ContextPacket:
        brief = project.db.get_brief()
        status = self.engine.status(project.root)
        status["recovery_learning"] = project.db.learning_guidance(role="coordinator")
        status["pending_work"] = [{key: item.get(key) for key in
            ("id", "kind", "reason", "status", "next_action", "chapter_no", "source", "intent", "progress", "resolution")}
            for item in project.pending_work()]
        status["pending_work_policy"] = (
            "待处理工作用于识别目标、进度、缺口和有依据的优化。Coordinator 安排白名单关联任务；"
            "原授权内补救自动衔接，新增优化先问，暂不处理保留。完成必须由实际产物和必要审核确认。"
            "可在 pending_work_proposals 提出至多三项新增任务，evidence 必须逐字来自本次输入，"
            "不能把自己的建议变成硬问题；没有依据不要提案。")
        status["latest_accepted_chapter"] = project.db.latest_accepted_chapter_no()
        status["draft_chapters"] = project.db.chapter_numbers_by_status("draft")
        status["ready_batches"] = self._ready_batch_ids(project)
        status["latest_pending_plan_revision"] = project.db.get_metadata("latest_pending_plan_revision")
        from .manual_edits import ManualEditsService
        status["manual_edit_questions"] = ManualEditsService(project).status()["issues"][:12]
        pending_planning = project.db.get_metadata("pending_planning_publication", {})
        if isinstance(pending_planning, dict) and pending_planning.get("run_id"):
            status["pending_planning_publication"] = {key: pending_planning.get(key)
                                                      for key in ("run_id", "anchor", "end", "focus")}
        try:
            active_planning = load_active_planning(project)
        except ValidationGateError as exc:
            active_planning = None
            status["formal_planning"] = {"status": "needs_attention", "reason": str(exc)}
        if active_planning is not None:
            manifest, _, _, window = active_planning
            status["formal_planning"] = {"revision_no": manifest.get("revision_no", 1),
                                         "accepted_anchor": window.anchor_chapter,
                                         "chapter_window": manifest["chapter_window"]}
        if re.search(r"旧版|历史规划|先前规划|以前的规划|恢复.*规划|融合.*规划|第\s*\d+\s*版", message):
            status["kept_planning_revisions"] = kept_revisions(project)
            status["planning_history_rule"] = "只在用户明确指定后读取某一保留版本的某一部分；旧版不是当前写作依据。"
        pending_question = self._pending_question(project)
        if pending_question:
            status["pending_user_question"] = pending_question
        pending_updates = project.db.get_metadata(conversation_key("pending_terminal_user_updates"), [])
        if isinstance(pending_updates, list) and pending_updates:
            status["pending_user_updates"] = [
                item for item in pending_updates[-20:]
                if isinstance(item, dict) and item.get("status") == "pending"
            ]
            status["pending_user_updates_policy"] = (
                "这些是运行中收到的用户原话，尚未应用到计划或正文；必须按当前消息重新路由，"
                "不得自动继续旧任务或重复执行。"
            )
        bundle = project.db.get_current_plan_bundle()
        if bundle is not None:
            status["current_arc"] = {
                "arc_id": bundle.current_arc.arc_id,
                "chapter_start": bundle.current_arc.chapter_start,
                "chapter_end": bundle.current_arc.chapter_end,
            }
        # Coordinator ???????????????????????
        # ????????????????????????????
        # ????????????????? token?
        queue_reconciliation = project.db.reconcile_collaboration_queue()
        status["queue_reconciliation"] = queue_reconciliation
        active_messages = project.db.list_collaboration_messages(active_only=True, limit=8)
        collaboration_digest = [
            {
                "message_id": item.get("message_id"),
                "thread_id": item.get("thread_id"),
                "message_type": item.get("message_type"),
                "chapter_no": item.get("chapter_no"),
                "chapter_version": item.get("chapter_version"),
                "sender_role": item.get("sender_role"),
                "recipient_role": item.get("recipient_role"),
                "status": item.get("status"),
                "claim": str(item.get("claim") or "")[:240],
                "evidence_refs": list(item.get("evidence_refs") or [])[:8],
                "created_at": item.get("created_at"),
            }
            for item in active_messages
        ]
        policy = {
            "roles": {
                "Coordinator": "理解需求、维护交流、拆解与派工；不写正文、不审批、不提交正史",
                "Writer": "按授权负责规划、正文与定向修订，提供版本绑定说明和候选",
                "Editor": "日常编辑与综合审查；按当前模式承担表达和记忆核对",
                "Reviewer": "已启用的专项逻辑连续性与证据审查，不代引擎接受",
                "Memory Keeper": "已启用的专项记忆核对与有据提案；Novel Engine 独占正史提交",
            },
            "hard_gates": [
                "不可直接修改 .inkflow/inkflow.db",
                "修订后必须重审",
                "当前模式的必要角色覆盖、证据、来源及配置核验通过并有接受授权，才可由 Novel Engine 接受",
                "accept 永远使用 force=false",
            ],
            "settings_boundary": "用户明确要求时，可以通过 Novel Engine 修改白名单设置；执行后必须返回旧值、新值和下一步建议。",
            "inquiry_frequency": self.engine.settings.inquiry_frequency,
            "preference_learning_enabled": project.db.get_metadata("learning_settings", {}).get("enabled", True),
            "inquiry_policy": {
                "low": "只追问缺少的执行条件和真实歧义",
                "medium": "低把握时追问",
                "high": "中等把握且存在重要创作分岔时追问",
                "ultra": "存在会明显改变成品的未知项就先追问；用户要求直接开始时不重复问",
            },
        }
        sections = [
            ContextSection(key="A", title="当前用户自然语言请求", content=message, hard=True),
            ContextSection(
                key="B",
                title="项目契约与用户规则",
                content=json_dumps(
                    {
                        "书名": brief.title,
                        "题材": brief.genre,
                        "单章目标字数": brief.target_chapter_words,
                        "用户规则": brief.user_rules,
                    }
                ),
                hard=True,
            ),
            ContextSection(key="C", title="当前项目状态", content=json_dumps(status), hard=True),
            ContextSection(key="D", title="不可绕过的会话边界", content=json_dumps(policy), hard=True),
            ContextSection(
                key="F",
                title="最近用户讨论与待确认事项",
                content=self._recent_dialogue(project, limit=3, char_limit=6_000)
                or "这是一次新的会话，尚无待确认事项。",
            ),
            ContextSection(
                key="F1",
                title="结构化 Agent 分歧与待用户回答问题",
                content=json_dumps(collaboration_digest),
                source_ids=[item["message_id"] for item in collaboration_digest if item.get("message_id")],
            ),
            ContextSection(
                key="E",
                title="输出契约",
                content=(
                    "只输出系统规定的会话路由结构；一次只选择一个受限工作流；"
                    "同时给出完整目标、识别把握、授权状态、缺失字段和必要澄清；"
                    "不输出小说正文或隐藏推理。"
                ),
                hard=True,
            ),
        ]
        from .story_settings import StorySettingsService
        sections.append(ContextSection(key="SET", title="用户定义的设定合集与职责（非正史）",
            content=StorySettingsService(project).context(actor="coordinator", max_chars=14000), hard=False))
        sections.append(preference_section(project.db))
        for section in sections:
            section.cache_scope = {"B": "book", "D": "global", "E": "global", "A": "request", "C": "request"}.get(section.key, section.cache_scope)
        packet = ContextPacket(
            project_id=project.project_id,
            chapter_no=1,
            task="理解用户的自然语言讨论、确认或执行请求，并路由到既有墨流工作流",
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
        )
        soft_limit, hard_limit = self.engine.settings.context_budget_for("coordinator")
        if packet.estimated_tokens > soft_limit:
            for key in ("F1", "F"):
                section = next((item for item in packet.sections if item.key == key), None)
                if section and len(section.content) > 1_000:
                    section.content = section.content[-max(1_000, len(section.content) // 2):]
                    packet.warnings.append(f"Coordinator 上下文接近预算，已缩短 {section.title}；项目硬状态未动。")
                    packet.estimated_tokens = estimate_tokens("\n".join(item.content for item in sections))
                    if packet.estimated_tokens <= soft_limit:
                        break
        if packet.estimated_tokens > hard_limit:
            raise ValidationGateError("Coordinator 的硬上下文超过自定义上限，请提高该 Agent 的最大上下文预算。")
        return packet

    @staticmethod
    def _dialogue_path(project: InkFlowProject) -> Path:
        current = active_conversation.get()
        return (project.root / "DIALOGUE.md" if current == "main" else
                project.internal / "conversations" / f"{current}.md")

    def _recent_dialogue(self, project: InkFlowProject, limit: int = 6, char_limit: int = 12_000) -> str:
        path = self._dialogue_path(project)
        if not path.is_file():
            return ""
        try:
            content = path.read_text(encoding="utf-8")
            if content.startswith("# 墨流对话记录\n\n"):
                content = content.removeprefix("# 墨流对话记录\n\n")
            entries = [item.strip() for item in content.split("\n\n---\n\n") if item.strip()]
        except OSError:
            return ""
        return "\n\n---\n\n".join(entries[-limit:])[-max(1_000, int(char_limit)):]

    @classmethod
    def history(cls, root: str | Path, limit: int = 100) -> list[dict[str, str]]:
        """Return user-visible dialogue only; never expose provider reasoning or trace data."""

        project = InkFlowProject(root, recover_on_open=False)
        path = cls._dialogue_path(project)
        if not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if content.startswith("# 墨流对话记录\n\n"):
            content = content.removeprefix("# 墨流对话记录\n\n")
        raw_entries = [item.strip() for item in content.split("\n\n---\n\n") if item.strip()]
        parsed: list[dict[str, str]] = []
        for index, entry in enumerate(raw_entries[-max(1, min(limit, 500)) :]):
            match = re.match(
                r"## 用户\n\n(?P<user>.*?)\n\n## (?:Coordinator|墨流会话主控)\n\n(?P<assistant>.*?)(?:\n\n> (?P<note>.*))?$",
                entry,
                flags=re.DOTALL,
            )
            if not match:
                continue
            note = (match.group("note") or "").strip()
            recorded_at = ""
            action_note = note
            if note.startswith("记录时间：") and " · " in note:
                recorded_at, action_note = note.removeprefix("记录时间：").split(" · ", 1)
            parsed.append(
                {
                    "id": f"dialogue-{len(raw_entries) - len(raw_entries[-max(1, min(limit, 500)) :]) + index + 1}",
                    "user": match.group("user").strip(),
                    "assistant": match.group("assistant").strip(),
                    "action_note": action_note,
                    "recorded_at": recorded_at,
                }
            )
        return parsed

    def _append_dialogue(
        self,
        project: InkFlowProject,
        user_message: str,
        intent: TerminalIntent,
        response: dict[str, Any],
    ) -> None:
        """Persist only user-visible conversation, never provider reasoning or keys."""

        failure = workflow_failure_reason(response)
        reply = str(
            failure
            or response.get("reply")
            or response.get("gate")
            or response.get("message")
            or response.get("summary")
            or intent.visible_reason
        ).strip()
        if failure:
            action_note = f"任务未完成，可继续恢复：{intent.action}"
        elif response.get("needs_clarification"):
            action_note = f"等待补充或确认：{intent.action}"
        else:
            action_note = "继续讨论" if intent.action == "discuss" else f"已路由：{intent.action}"
        self._append_dialogue_entry(project, user_message, reply, action_note)

    def _append_dialogue_entry(
        self,
        project: InkFlowProject,
        user_message: str,
        reply: str,
        action_note: str,
        *,
        force: bool = False,
    ) -> bool:
        """按用户设置追加一条可见对话记录；返回本次是否真正写入文件。"""

        settings = self.engine.settings
        if not force:
            if settings.dialogue_history_mode == "manual":
                return False
            interval = max(1, settings.dialogue_history_interval)
            if interval > 1:
                # 自动保存按“每 N 轮”计数；计数放在项目元数据里，不新增文件。
                pending = int(project.db.get_metadata(conversation_key("dialogue_turns_since_save"), 0) or 0) + 1
                if pending < interval:
                    project.db.set_metadata(conversation_key("dialogue_turns_since_save"), pending)
                    return False
        project.db.set_metadata(conversation_key("dialogue_turns_since_save"), 0)
        self._write_dialogue_entry(
            project,
            user_message,
            reply,
            action_note,
            settings.dialogue_history_limit,
        )
        return True

    @classmethod
    def save_manual_entry(
        cls,
        root: str | Path,
        user_message: str,
        reply: str,
        action_note: str = "用户手动保存",
    ) -> dict[str, Any]:
        """用户明确要求时保存一条记录，不受“被动/主动”设置限制。"""

        project = InkFlowProject(root)
        message = user_message.strip()
        answer = reply.strip()
        if not message or not answer:
            raise ValueError("没有可保存的对话内容。")
        settings = Settings.from_env(project.root)
        cls._write_dialogue_entry(
            project, message, answer, action_note, settings.dialogue_history_limit
        )
        project.db.set_metadata(conversation_key("dialogue_turns_since_save"), 0)
        return {
            "saved": True,
            "entries": len(cls._dialogue_entries(project)),
            "limit": settings.dialogue_history_limit,
        }

    @staticmethod
    def _dialogue_entries(project: InkFlowProject) -> list[str]:
        """读取 DIALOGUE.md 的原始条目；不截断长度。"""

        path = TerminalSession._dialogue_path(project)
        if not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if content.startswith("# 墨流对话记录\n\n"):
            content = content.removeprefix("# 墨流对话记录\n\n")
        return [item.strip() for item in content.split("\n\n---\n\n") if item.strip()]

    @staticmethod
    def _write_dialogue_entry(
        project: InkFlowProject,
        user_message: str,
        reply: str,
        action_note: str,
        limit: int,
    ) -> None:
        """写入一条记录，并按保留上限裁剪旧记录。"""

        recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = (
            f"## 用户\n\n{user_message}\n\n## Coordinator\n\n{reply}"
            f"\n\n> 记录时间：{recorded_at} · {action_note}"
        )
        entries = TerminalSession._dialogue_entries(project)
        entries.append(entry)
        keep = max(1, limit)
        atomic_write_text(
            TerminalSession._dialogue_path(project),
            "# 墨流对话记录\n\n" + "\n\n---\n\n".join(entries[-keep:]) + "\n",
        )

    async def _dispatch(
        self, root: Path, intent: TerminalIntent,
        ticket: TaskTicket | None = None, dispatch_plan: DispatchPlan | None = None,
        *, packet: ContextPacket | None = None,
        consume_steering: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> dict[str, Any]:
        # The executed action must be the same immutable, validated workflow
        # shown to the user. The engine owns each step's production gates.
        if active_task_settings.get() is None:
            project = InkFlowProject(root)
            scope = StudioService(project).db.prepare_task_settings(
                f"dispatch-{uuid4().hex}", novel_id=project.project_id,
                settings=self.engine.settings, workspace_root=project.root,
                collaboration_mode=dispatch_plan.collaboration_mode if dispatch_plan else "everyday",
            )
            with use_task_settings(scope):
                return await self._dispatch(
                    root, intent, ticket, dispatch_plan,
                    packet=packet, consume_steering=consume_steering,
                )
        if ticket is None or dispatch_plan is None:
            task_scope = active_task_settings.get()
            ticket, dispatch_plan = Coordinator(InkFlowProject(root, recover_on_open=False)).compile(
                intent,
                collaboration_mode=task_scope.collaboration_mode if task_scope else "everyday",
                task_snapshot_hash=task_scope.snapshot_hash if task_scope else None,
            )
        Coordinator.validate(dispatch_plan, ticket)
        if dispatch_plan.workflow != intent.action:
            raise InkFlowError("任务计划与实际工作流不一致，已停止执行。")
        task_scope = active_task_settings.get()
        if task_scope is not None and (
            dispatch_plan.collaboration_mode != task_scope.collaboration_mode
            or dispatch_plan.task_snapshot_hash != task_scope.snapshot_hash
        ):
            raise InkFlowError("工作流计划与本次任务快照不一致，已停止执行。")
        forbidden = set(intent.forbidden_actions)
        if (("write" in forbidden and intent.action in {
                "scene_draft", "write_draft", "write_review", "write_review_accept",
                "batch_draft", "batch_draft_accept", "batch_repair", "continue_run",
            }) or ("review" in forbidden and intent.action in {
                "review", "review_accept", "write_review", "write_review_accept",
                "revise_review", "revise_review_accept", "batch_draft", "batch_draft_accept",
                "batch_repair", "continue_run",
            }) or ("accept" in forbidden and intent.action in {
                "accept", "review_accept", "write_review_accept", "revise_review_accept",
                "batch_accept", "batch_draft_accept", "continue_run",
            })):
            return {"gate": "这项工作流会触及你本轮明确禁止的写作、审查或验收动作；没有执行。请确认保留哪些步骤。"}
        if (
            intent.action not in _READ_ONLY_ACTIONS
            and intent.action != "discuss"
            and intent.action != "chat"
            and intent.authorization != "approved"
        ):
            return {
                "gate": (
                    "这条消息还没有明确要求现在执行。你可以用自己的说法表示开始，"
                    "例如“行，就照刚才说的做”；不需要固定口令。"
                )
            }
        if intent.action == "exit":
            return {"ended": True, "message": "会话已结束。"}
        if intent.action == "help":
            return {
                "help": [
                    "不需要背命令：例如“帮我瞅瞅现在写到哪儿了”也能查看状态",
                    "为第 1 章生成规划",
                    "当前篇章完成后，规划下一个篇章",
                    "继续按完整门禁写作，直到已接受正文达到 10 万汉字",
                    "按章节卡写第 1 章并审查",
                    "生成第 11 到第 30 章的独立大纲（只写入 planning/outlines）",
                    "根据审查意见修订第 1 章，复审；仅通过才接受",
                    "审查第 1 章，通过后入正史",
                    "只尝试接受第 1 章，不要 force",
                    "查看当前项目状态",
                    "列出当前所有回退点",
                    "为当前状态创建一个名为‘第一篇章完成’的检查点",
                    "预览回退到生成第 2 章之前会影响哪些文件",
                    "使用预览返回的确认码确认回退",
                ]
            }
        if intent.action == "status":
            return {"result": self.engine.status(root)}
        if intent.action == "pending_work":
            project = InkFlowProject(root, recover_on_open=False)
            async with project_write_lock(root):
                project.reconcile_pending_work(current_pass=lambda number: self.engine.current_pass_review(project, number),
                                               current_source=self._work_source(project))
            items = project.pending_work()
            if intent.pending_work_decision == "process_all":
                results = []
                initial_ids = [item["id"] for item in items if item.get("can_process") and item["status"] != "deferred"]
                for identity in initial_ids:
                    item = next((work for work in project.pending_work() if work["id"] == identity), None)
                    if item is None or not item.get("can_process"):
                        continue
                    try:
                        message = f"处理待办 {identity}"
                        pending = self.pending_resume(project, message)
                        if pending and pending.get("run_id"):
                            scope = StudioService(project).db.prepare_task_settings(f"pending-{uuid4().hex}",
                                novel_id=project.project_id, settings=self.engine.settings, workspace_root=root,
                                resume_run_id=pending["run_id"])
                            previous_engine, runtime = self.engine, active_runtime.get()
                            previous_ids = (runtime.run_id, runtime.task_id) if runtime else None
                            self.engine = InkFlowEngine(create_provider(scope.settings), scope.settings)
                            try:
                                with use_task_settings(scope):
                                    if runtime:
                                        runtime.run_id, runtime.task_id = pending["run_id"], scope.task_id
                                    result = await self.handle(root, message, opened_project=project)
                            finally:
                                self.engine = previous_engine
                                if runtime and previous_ids:
                                    runtime.run_id, runtime.task_id = previous_ids
                        else:
                            result = await self.handle(root, message, opened_project=project)
                    except InkFlowError as exc:
                        results.append({"id": identity, "status": "waiting_condition", "reason": str(exc)})
                        break  # A provider/resource failure also affects the remaining items; do not repeat it.
                    results.append({"id": identity, "status": workflow_result_status(result),
                                    "reason": workflow_failure_reason(result) or str(result.get("reply", ""))})
                    if workflow_result_status(result) != "completed":
                        break
                remaining = project.pending_work()
                return {"status": "waiting_condition" if remaining else "completed", "pending_work": remaining,
                        "processed_items": results,
                        "reply": f"本次待办收尾已结束：处理 {len(results)} 项，当前剩余 {len(remaining)} 项。" +
                            ("未完成项保留具体断点和原因，已停止重复执行。" if remaining else "当前待办全部完成，无需再次处理。"),
                        "next_action": results[-1].get("reason", "查看具体待办条件") if remaining and results else "查看当前待办与已完成结果"}
            if intent.pending_work_id:
                items = [item for item in items if item["id"] == intent.pending_work_id]
                if intent.pending_work_decision == "defer" and items:
                    project.update_pending_work(items[0]["id"], status="deferred", user_decision=intent.user_message)
                    pending = self._pending_question(project)
                    if pending and any(card.get("work_id") == items[0]["id"] for card in pending.get("questions", [])):
                        project.db.set_metadata(conversation_key("pending_terminal_question"), None)
                    return {"status": "completed", "pending_work": project.pending_work(), "reply": "已保留这项待办及进度到下次对话，本次安排已结束。"}
            if intent.pending_work_decision == "process":
                item = next((item for item in items if item.get("can_process")), items[0] if items else None)
                if item is None:
                    if intent.pending_work_id:
                        previous = project.db.get_metadata(f"pending_work:{intent.pending_work_id}", {})
                        if previous.get("status") not in {"completed", "superseded"}:
                            return {"status": "waiting_condition", "reply": "未找到这项待办的当前来源，未执行或宣称完成。",
                                    "next_action": "刷新待办列表，选择当前具体事项。", "pending_work": project.pending_work()}
                    return {"status": "completed", "pending_work": project.pending_work(), "reply": "这项待办已处理或已由新版本替代，当前无需重复执行。"}
                if not item.get("can_process") or item.get("intent"):
                    return {"status": "waiting_condition", "reply": f"已保留待办：{item['reason']}。当前没有可安全执行的恢复入口或自动尝试已耗尽。",
                            "next_action": item["next_action"], "pending_work": items}
                if item["handling"] == "manual":
                    from .manual_edits import ManualEditsService
                    job = await ManualEditsService(project).review(item["source"]["job_id"], self.engine, resume=True)
                    passed = job["status"] in {"approved", "unchanged", "passed", "reconciled"}
                    return {"status": "completed" if passed else "waiting_user", "result": job,
                            "reply": job.get("summary", "候选核对已返回"), "pending_work": project.pending_work(),
                            "next_action": "在手动修改核对窗口查看依据并选择；原候选与尝试次数保留。"}
                async with project_write_lock(project.root):
                    current = next((work for work in project.pending_work() if work["id"] == item["id"]), None)
                    if current is None or not current.get("can_process"):
                        return {"status": "waiting_condition", "reply": "待办来源或状态已变化，请刷新后查看。"}
                    project.update_pending_work(item["id"], processing_attempts=int(current.get("processing_attempts", 0)) + 1)
                if item["handling"] == "local":
                    async with project_write_lock(project.root):
                        project.recover_open_state()
                    result = {"warnings": project.recovery_warnings}
                else:
                    result = await self.engine.review_chapter_mode(root, int(item["chapter_no"]),
                        mode=dispatch_plan.collaboration_mode, instruction="从当前版本补齐待办审查：" + item["reason"])
                    project.reconcile_pending_work(current_pass=lambda number: self.engine.current_pass_review(project, number))
                remaining = next((work for work in project.pending_work() if work["id"] == item["id"]), None)
                return {"status": "waiting_condition" if remaining else "completed", "result": result,
                        "reply": "已从原节点处理，本次自动执行已收尾。" if not remaining else "本次定点处理已结束，尚缺必要条件，已停止重复执行并保留断点。",
                        "next_action": remaining["next_action"] if remaining else "查看已完成结果",
                        "pending_work": project.pending_work()}
            response = {"pending_work": items, "reply": "当前没有待处理工作。" if not items else
                        "待处理工作：\n" + "\n".join(f"{item['id']} · {item['status']}：{item['reason']}\n下一步：{item['next_action']}" for item in items)}
            # A fixed answer is bound to this exact item and routed as its saved workflow.
            self._pending_work_question(project, response, include_deferred=True)
            return response
        if intent.action == "scene_draft":
            if "write" in intent.forbidden_actions:
                return {"gate": "你要求本轮不要写正文；没有生成场景草稿。可以先继续讨论。"}
            scene_packet = packet or self._build_packet(InkFlowProject(root), intent.user_message or intent.operation_instruction)
            return {"result": await self.engine.draft_scene(
                root, intent.operation_instruction or intent.user_message,
                scene_packet, chapter_no=intent.chapter_no,
            )}
        if intent.action == "story_settings_manage":
            return {"result": await self.engine.manage_story_settings(root, change=intent.setting_change,
                instruction=intent.operation_instruction or intent.user_message)}
        if intent.action == "story_setting_edit":
            if intent.document_kind not in {"book", "outline", "story_detail"}:
                return {"gate": "请明确要修改书籍设定、大纲还是剧情细纲；不会猜测修改范围。"}
            return {"result": await self.engine.edit_story_setting(
                root, document_kind=intent.document_kind,
                setting_change=intent.setting_change,
                instruction=intent.operation_instruction,
            )}
        if intent.action == "revise_selection":
            if re.search(r"这段留着|保留这段", intent.user_message):
                return {"gate": "我会保留你说的这段。请选中要改的结尾，或贴出结尾的准确原文；不会把要保留的文字当成修改目标。"}
            project = InkFlowProject(root)
            chapter = project.db.get_chapter(intent.chapter_no or 0)
            if not chapter or chapter["status"] != "draft":
                return {"gate": "只改选区需要一份未验收的当前草稿；已接受正文须走带版本核对的局部修订。"}
            relative = str(chapter["path"])
            if not relative.endswith(".draft.md"):
                return {"gate": "当前草稿路径不属于可局部修订的文件，未改正文。"}
            excerpt = intent.target_excerpt.strip()
            if not excerpt:
                return {"gate": "我会保留其余内容。请在草稿中选中要改的原文，或把那段原文完整贴给我；不会重写整章。"}
            document = StudioService(project).read_document(relative)
            content = str(document["content"])
            if content.count(excerpt) != 1:
                return {"gate": "指定原文在当前草稿中没有唯一匹配。请选中准确文字或贴出完整原句；未改正文。"}
            start = content.index(excerpt)
            return {"result": await self.engine.revise_selection(
                root, relative, start, start + len(excerpt),
                intent.operation_instruction or intent.user_message,
                expected_hash=str(document["content_hash"]),
            )}
        if intent.action == "settings_update":
            patch = self._sanitize_settings_patch(intent.settings_patch)
            if not patch:
                return {"gate": "我还没有识别出可执行的设置项。请说清要改哪项，例如“把章节长度容错改为 30%”。"}
            before = Settings.from_env(root)
            saved = save_user_settings(patch)
            after = Settings.from_env(root)
            changes = {
                key: {"before": getattr(before, key, None), "after": getattr(after, key, saved.get(key))}
                for key in patch
                if getattr(before, key, None) != getattr(after, key, saved.get(key))
            }
            unchanged = [
                key
                for key in patch
                if key not in changes
            ]
            change_summary = [
                self._setting_change_summary(key, item["before"], item["after"])
                for key, item in changes.items()
            ]
            if change_summary:
                summary = "已修改设置：" + "；".join(change_summary)
            elif unchanged:
                summary = "设置没有变化：" + "、".join(self._setting_label(key) for key in unchanged)
            else:
                summary = "没有识别到可保存的设置变化。"
            return {
                "settings_updated": changes,
                "settings_unchanged": unchanged,
                "settings_change_summary": change_summary,
                "summary": summary,
                "next_action": "新的设置会用于下一次任务；你可以继续说要做什么，Coordinator 会按当前设置安排。",
            }
        if intent.action == "voice_clone_script":
            return {"result": await self.engine.generate_voice_clone_script(root)}
        if intent.action == "plan":
            return {
                "steps": [
                    {
                        "step": "writer.plan",
                        "result": await self.engine.generate_plan(
                            root,
                            instruction=intent.operation_instruction,
                            chapter_range=(intent.chapter_no, intent.end_chapter_no)
                            if intent.chapter_no is not None and intent.end_chapter_no is not None
                            else None,
                        ),
                    }
                ]
            }
        if intent.action == "plan_preview":
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "批次规划预览需要明确起止章节，例如“查看第 7～11 章规划”。"}
            return {
                "result": self.engine.preview_plan_range(root, intent.chapter_no, intent.end_chapter_no),
                "next_action": "你可以一次确认全部，或只指出需要调整的章节号；当前操作没有修改规划。",
            }
        if intent.action == "planning_history_view":
            original = next((section.content for section in packet.sections if section.key == "A"),
                            intent.user_message) if packet else intent.user_message
            if not re.search(r"旧版|历史|以前|先前|之前|前面|参考|融合|第\s*\d+\s*版", original):
                return {"gate": "没有收到你主动查看旧版规划的指令，历史内容未读取。"}
            project = InkFlowProject(root)
            if intent.planning_revision_no is None:
                return {"revisions": kept_revisions(project),
                        "next_action": "请指定要看的旧版修订号，以及大纲、卷细纲或近期规划。"}
            if intent.planning_part == "none":
                return {"gate": "请指定想看旧版的大纲、卷细纲还是近期规划。"}
            return {"result": read_kept_part(project, intent.planning_revision_no, intent.planning_part,
                                              chapter_no=intent.planning_reference_chapter_no,
                                              volume_no=intent.planning_reference_volume_no)}
        if intent.action == "planning_history_restore":
            original = next((section.content for section in packet.sections if section.key == "A"),
                            intent.user_message) if packet else intent.user_message
            if not re.search(r"恢复|回到|切回|重新采用", original):
                return {"gate": "恢复历史版必须由你明确提出，当前生效规划未改。"}
            if intent.planning_revision_no is None:
                return {"gate": "请明确要恢复的历史版本号；不会猜测。"}
            return {"result": await asyncio.to_thread(restore_kept_planning_publication,
                                                        root, intent.planning_revision_no)}
        if intent.action == "planning_publish_reviewed":
            original = next((section.content for section in packet.sections if section.key == "A"),
                            intent.user_message) if packet else intent.user_message
            if not re.search(r"确认采用|正式采用|就采用|采用刚才|发布刚才|让.*生效|确认正式采用这版规划", original):
                return {"gate": "没有收到正式采用已审核规划的明确指令，当前版未改变。"}
            pending = InkFlowProject(root).db.get_metadata("pending_planning_publication", {})
            if not isinstance(pending, dict) or not pending.get("run_id"):
                return {"gate": "没有等待确认的已审核三层候选；当前正式版未改变。"}
            if intent.checkpoint_id and intent.checkpoint_id != pending["run_id"]:
                return {"gate": "你指定的候选与当前待确认版本不同，请先核对运行编号。"}
            published = await self.engine.redesign_story(
                root, anchor=int(pending["anchor"]), end=int(pending["end"]),
                instruction=str(pending["instruction"]), focus=str(pending.get("focus") or ""),
                approved_run_id=str(pending["run_id"]),
            )
            async with project_write_lock(root):
                project = InkFlowProject(root)
                pending_task = project.db.get_metadata(conversation_key("pending_creation_task"), {})
                if (isinstance(pending_task, dict) and pending_task.get("status") == "waiting_user"
                        and pending_task.get("intent", {}).get("action") == "redesign_story"):
                    if workflow_result_status({"result": published}) == "completed":
                        project.db.set_metadata(conversation_key("pending_creation_task"), {**pending_task, "status": "completed"})
                        for item in project.pending_work():
                            if (item["kind"] == "workflow" and item.get("run_id") == pending_task.get("run_id")
                                    and item["source"].get("task_id") == pending_task.get("task_id")
                                    and item["source"].get("conversation") == conversation_key("pending_creation_task")):
                                project.update_pending_work(item["id"], status="completed",
                                    resolution={"publication_run_id": published.get("trace_id"), "user_decision": original})
            return {"result": published}
        if intent.action == "redesign_story":
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "请明确以哪一章已接受正文为起点、规划到第几章；例如‘以第15章为基础规划到第24章’。"}
            planning_instruction = intent.operation_instruction or intent.user_message
            original = next((section.content for section in packet.sections if section.key == "A"),
                            intent.user_message) if packet else intent.user_message
            if (re.search(r"(?:继续|恢复|接着|重试|续修).{0,30}(?:未完成|中断|断点|候选|失败)|断点.{0,12}续修", original)
                    and not re.search(r"继续.{0,18}未完成|断点.{0,8}续修", planning_instruction)):
                planning_instruction = "继续未完成的规划。\n" + planning_instruction
            if (intent.planning_revision_no is not None
                    and not re.search(r"旧版|历史|以前|先前|之前|前面|参考|融合|恢复|第\s*\d+\s*版", original)):
                intent = intent.model_copy(update={"planning_revision_no": None, "planning_part": "none",
                                            "planning_reference_chapter_no": None,
                                            "planning_reference_volume_no": None})
            if intent.planning_revision_no is not None:
                if intent.planning_part == "none":
                    return {"gate": "请指定要参考旧版的大纲、卷细纲或近期规划；不会默认引用整版。"}
                original = next((section.content for section in packet.sections if section.key == "A"),
                                intent.user_message) if packet else intent.user_message
                if not re.search(r"旧版|历史|以前|先前|之前|前面|参考|融合|恢复|第\s*\d+\s*版", original):
                    return {"gate": "只有你明确要求参考旧版时才读取历史规划。"}
                historical = read_kept_part(InkFlowProject(root), intent.planning_revision_no,
                                            intent.planning_part,
                                            chapter_no=intent.planning_reference_chapter_no,
                                            volume_no=intent.planning_reference_volume_no)
                planning_instruction += (f"\n【用户指定的第 {intent.planning_revision_no} 版{intent.planning_part}非正史参考，"
                                         "仅提取与本次目标相关的创作方向，不把旧未来当事实】\n"
                                         + historical["content"])
            return {"result": await self.engine.redesign_story(
                root, anchor=intent.chapter_no, end=intent.end_chapter_no,
                instruction=planning_instruction,
                focus="chapter-window" if _focused_recent_plan_request(intent.operation_instruction or intent.user_message) else "",
            )}
        if intent.action == "outline":
            if intent.outline_level == "detail":
                detail_result = await self.engine.generate_story_detail(root, instruction=intent.operation_instruction)
                return {"result": detail_result, "next_action": detail_result.get("next_action") or
                        "剧情细纲候选已保存，按实际审核和发布状态继续。"}
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "独立大纲需要明确起止章节，例如“生成第 11 到第 30 章大纲”。"}
            outline_result = await self.engine.generate_outline(
                    root,
                    intent.chapter_no,
                    intent.end_chapter_no,
                    instruction=intent.operation_instruction,
                    outline_level=intent.outline_level,
                )
            if outline_result.get("status") in {"planned", "unchanged", "waiting_user"}:
                return {"result": outline_result, "next_action": outline_result.get("next_action", "核对当前正式规划与受影响的下游审核结果。")}
            chapter_summaries = outline_result.get("outline", {}).get("chapters", [])
            opening = chapter_summaries[0].get("title") if chapter_summaries else ""
            ending = chapter_summaries[-1].get("title") if chapter_summaries else ""
            summary = f"第 {intent.chapter_no}～{intent.end_chapter_no} 章候选大纲已保存，还没有得到你的确认。"
            if opening and ending:
                summary += f"\n本版从《{opening}》展开，到《{ending}》收束；具体因果与 Writer 的说明请打开大纲审读。"
            summary += "\n我还没有展开细纲、修改近期计划或写正文。"
            return {
                "result": outline_result,
                "reply": summary,
                "next_action": "请先审读并确认大纲走向；确认后再询问是否展开剧情细纲，先不切分章节。",
            }
        if intent.action == "plan_next_arc":
            if (
                intent.operation_instruction.strip()
                and not intent.plan_change_confirmed
                and intent.authorization != "approved"
            ):
                return {
                    "gate": (
                        "这会改变未来篇章规划。请先阅读篇章复审报告；若同意，"
                        "用任何清楚的说法告诉我现在执行即可，不需要固定口令。"
                    )
                }
            return {
                "steps": [
                    {
                        "step": "writer.plan.advance",
                        "result": await self.engine.advance_plan(root, instruction=intent.operation_instruction),
                    }
                ]
            }
        if intent.action == "plan_brief":
            return {
                "steps": [
                    {
                        "step": "writer.plan.brief",
                        "result": await self.engine.preview_next_arc(
                            root,
                            instruction=intent.operation_instruction,
                        ),
                    }
                ]
            }
        if intent.action == "arc_audit":
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "篇章复审需要明确起止章节，例如“复审第 1 到第 10 章并和规划对比”。"}
            return {
                "result": await self.engine.audit_range(
                    root,
                    intent.chapter_no,
                    intent.end_chapter_no,
                    batch_id=intent.batch_id,
                )
            }
        if intent.action in {"batch_draft", "batch_draft_accept"}:
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "批量草稿必须明确起止章节，例如“生成第 9 到第 10 章的批量草稿”。"}
            drafted = await self.engine.draft_batch(
                root,
                intent.chapter_no,
                intent.end_chapter_no,
                instruction=intent.operation_instruction,
                max_revision_rounds=intent.max_revision_rounds,
                batch_id=intent.batch_id,
                consume_steering=consume_steering,
            )
            if intent.action == "batch_draft_accept" and drafted.get("status") == "ready_for_acceptance":
                accepted = await self.engine.accept_batch(root, str(drafted["batch_id"]))
                return {
                    "result": accepted,
                    "batch_draft": drafted,
                    "authorization_source": intent.authorization_source,
                }
            return {"result": drafted}
        if intent.action == "batch_repair":
            if not intent.batch_id or intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "修复临时批次需要可确定的批次和章节范围；请说明批次编号或第 N 到第 M 章。"}
            return {
                "result": await self.engine.repair_batch(
                    root,
                    intent.batch_id,
                    start_chapter_no=intent.chapter_no,
                    end_chapter_no=intent.end_chapter_no,
                    instruction=intent.operation_instruction,
                    max_additional_revision_rounds=intent.max_revision_rounds,
                    review_only=intent.batch_review_only,
                )
            }
        if intent.action == "batch_accept":
            if not intent.batch_id:
                return {"gate": "接收批次必须提供批次编号，例如“接收批次 batch-……”。"}
            return {"result": await self.engine.accept_batch(root, intent.batch_id)}
        if intent.action == "continue_run":
            if intent.target_characters is None and intent.end_chapter_no is None:
                return {"gate": "连续长跑必须明确终点，例如“到 10 万汉字”或“连续完成到第 10 章”。"}
            if intent.target_characters is not None and intent.end_chapter_no is not None:
                return {"gate": "一次长跑只能使用一种终点：字符数或结束章节号。请明确保留其中一个。"}
            return {
                "result": await self.engine.continue_until(
                    root,
                    intent.target_characters,
                    instruction=intent.operation_instruction,
                    target_chapter_no=intent.end_chapter_no,
                    max_revision_rounds=intent.max_revision_rounds,
                    consume_steering=consume_steering,
                )
            }
        if intent.action == "checkpoint_list":
            return {"result": self.engine.checkpoint_list(root)}
        if intent.action == "checkpoint_create":
            label = intent.operation_instruction.strip() or "用户手动检查点"
            return {"result": self.engine.checkpoint_create(root, label)}
        if intent.action == "rollback_preview":
            return {
                "result": self.engine.rollback_preview(
                    root,
                    checkpoint_id=intent.checkpoint_id,
                    boundary_chapter=intent.chapter_no,
                )
            }
        if intent.action == "rollback_restore":
            if not intent.confirmation_token:
                return {"gate": "确认回退前必须先预览，并逐字提供 confirmation_token。"}
            return {
                "result": self.engine.rollback_restore(
                    root,
                    checkpoint_id=intent.checkpoint_id,
                    boundary_chapter=intent.chapter_no,
                    confirmation_token=intent.confirmation_token,
                )
            }
        if intent.action in _CHAPTER_ACTIONS and intent.chapter_no is None:
            return {"gate": "这条请求需要明确章节号，例如“第 1 章”。未猜测章节。"}

        chapter_no = int(intent.chapter_no or 1)
        instruction = intent.operation_instruction
        steps: list[dict[str, Any]] = []

        if intent.action in {"accept", "review_accept", "write_review_accept", "revise_review_accept"}:
            project = InkFlowProject(root)
            scope = active_task_settings.get()
            chapter = project.db.get_chapter(chapter_no)
            receipt = project.db.get_metadata(f"workflow_accept:{scope.task_id}:{chapter_no}", {}) if scope else {}
            if chapter and chapter["status"] == "accepted" and receipt:
                return {"steps": [await self._run_step("memory.accept",
                    self.engine.accept_chapter(root, chapter_no, force=False))]}

        if intent.action == "write_draft":
            written = self._reuse_workflow_writer(InkFlowProject(root), chapter_no, "write", instruction)
            steps.append(written or await self._run_step("writer.write", self.engine.write_chapter(root, chapter_no, instruction)))
            return {
                "steps": steps,
                "next_action": "草稿已生成；你可以先阅读章节文件，确认后再说“审查第 N 章”或“按意见修改第 N 章”。",
            }

        if intent.action in {"write_review", "write_review_accept"}:
            written = self._reuse_workflow_writer(InkFlowProject(root), chapter_no, "write", instruction)
            written = written or await self._run_step(
                "writer.write", self.engine.write_chapter(root, chapter_no, instruction)
            )
            steps.append(written)
            if "gate" in written:
                return {"steps": steps}
            reviewed = self._reuse_current_pass_review(InkFlowProject(root), chapter_no)
            reviewed = reviewed or await self._run_step("engine.review_mode", self.engine.review_and_repair(
                root, chapter_no, instruction=instruction, max_revision_rounds=intent.max_revision_rounds,
            ))
            steps.append(reviewed)
            if "gate" in reviewed or intent.action == "write_review":
                return {"steps": steps}
            return await self._conditionally_accept(root, chapter_no, steps, reviewed)

        if intent.action == "revise_draft":
            revised = self._reuse_workflow_writer(InkFlowProject(root), chapter_no, "revise", instruction)
            steps.append(revised or await self._run_step("writer.revise", self.engine.revise_chapter(root, chapter_no, instruction)))
            return {
                "steps": steps,
                "next_action": "修订草稿已生成；它尚未重审或进入正史。",
            }

        if intent.action == "repair_accepted":
            steps.append(await self._run_step(
                "engine.repair_accepted",
                self.engine.repair_accepted_continuity(root, chapter_no, instruction),
            ))
            return {"steps": steps, **({"result": steps[0]["result"]} if "result" in steps[0]
                                      else {"gate": steps[0]["gate"]})}

        if intent.action in {"revise_review", "revise_review_accept"}:
            project = InkFlowProject(root)
            record = project.db.latest_review_record(chapter_no)
            chapter = project.db.get_chapter(chapter_no)
            # Repair the workflow dependency instead of stopping because the
            # Coordinator requested revision before there was a current review.
            current_review = bool(record and chapter
                and record["chapter_version"] == chapter["version"]
                and record["report"].source_hash == chapter["content_hash"])
            if current_review:
                revised = self._reuse_workflow_writer(project, chapter_no, "revise", instruction)
                revised = revised or await self._run_step(
                    "writer.revise", self.engine.revise_chapter(root, chapter_no, instruction)
                )
                steps.append(revised)
                if "gate" in revised:
                    return {"steps": steps}
            reviewed = self._reuse_current_pass_review(InkFlowProject(root), chapter_no)
            reviewed = reviewed or await self._run_step("engine.review_mode", self.engine.review_and_repair(
                root, chapter_no, instruction=instruction, max_revision_rounds=intent.max_revision_rounds,
            ))
            steps.append(reviewed)
            if "gate" in reviewed or intent.action == "revise_review":
                return {"steps": steps}
            return await self._conditionally_accept(root, chapter_no, steps, reviewed)

        if intent.action == "review":
            review = self.engine.review_chapter_mode(
                root, chapter_no, mode=dispatch_plan.collaboration_mode,
                instruction=instruction,
            )
            steps.append(await self._run_step(
                "engine.review_mode",
                review,
            ))
            return {
                "steps": steps,
                "next_action": "本次只处理审查；记忆候选取决于结论与当前模式。是否入正史仍按接受授权和引擎门禁决定。",
            }

        if intent.action == "review_accept":
            # Coordinator 先检查当前草稿是否已经有同版本、同正文哈希的
            # pass 报告。用户明确是在已有合格版本上接收时，不重复调用
            # Reviewer；这避免无意义的重复等待，同时仍由 accept_chapter
            # 再次校验版本、哈希、结论和 记忆服务 门禁。
            reused = self._reuse_current_pass_review(InkFlowProject(root), chapter_no)
            if reused is not None:
                steps.append(reused)
                return await self._conditionally_accept(root, chapter_no, steps, reused)
            reviewed = await self._run_step("engine.review_mode", self.engine.review_and_repair(
                root, chapter_no, instruction=instruction, max_revision_rounds=intent.max_revision_rounds,
            ))
            steps.append(reviewed)
            if "gate" in reviewed:
                return {"steps": steps}
            return await self._conditionally_accept(root, chapter_no, steps, reviewed)

        if intent.action == "accept":
            steps.append(
                await self._run_step(
                    "memory.accept", self.engine.accept_chapter(root, chapter_no, force=False)
                )
            )
            return {"steps": steps}

        return {"gate": f"不支持的路由结果：{intent.action}"}

    @staticmethod
    def _setting_label(key: str) -> str:
        return _SETTING_LABELS.get(key, key)

    @staticmethod
    def _setting_value_label(key: str, value: Any) -> str:
        if key in {"chapter_length_tolerance", "review_min_confidence"}:
            try:
                return f"{float(value) * 100:g}%"
            except (TypeError, ValueError):
                return str(value)
        if key in {"voice_auto_read", "voice_output_enabled", "voice_input_enabled"}:
            return "开启" if bool(value) else "关闭"
        if key in {"context_soft_tokens", "context_hard_tokens"}:
            try:
                return f"{int(value):,} tokens"
            except (TypeError, ValueError):
                return str(value)
        if key == "agent_generation":
            if isinstance(value, dict):
                roles = "、".join(str(role) for role in value)
                return f"已调整（{roles or '当前角色'}）"
            return str(value)
        mapped = _SETTING_VALUE_LABELS.get(key, {})
        return str(mapped.get(str(value), value))

    @staticmethod
    def _setting_change_summary(key: str, before: Any, after: Any) -> str:
        if key == "agent_generation" and isinstance(before, dict) and isinstance(after, dict):
            role_labels = {"coordinator": "Coordinator", "writer": "Writer", "editor": "Editor", "reviewer": "Reviewer", "memory_keeper": "Memory Keeper"}
            fields = ("temperature", "top_p", "top_k")
            items: list[str] = []
            for role, label in role_labels.items():
                old_values = before.get(role) if isinstance(before.get(role), dict) else {}
                new_values = after.get(role) if isinstance(after.get(role), dict) else {}
                for field in fields:
                    if old_values.get(field) != new_values.get(field):
                        items.append(f"{label} {field} {old_values.get(field)} → {new_values.get(field)}")
            if items:
                return "Agent 生成参数：" + "、".join(items)
        return f"{TerminalSession._setting_label(key)}：{TerminalSession._setting_value_label(key, before)} → {TerminalSession._setting_value_label(key, after)}"

    @staticmethod
    def _recommend_next_step(intent: TerminalIntent, response: dict[str, Any]) -> dict[str, str] | None:
        """Return one short, user-controlled next action for the Coordinator UI."""

        if response.get("resumable") is True and response.get("status") == "interrupted":
            return {"label": "继续断点", "reason": "执行片段已到上限，已保存成果可复用。", "prompt": "继续上次任务"}
        if response.get("needs_clarification"):
            return None
        if workflow_result_status(response) == "waiting_user":
            if response.get("questions"):
                return {"label": "回答待处理选择", "reason": "已保留当前成果；回答这张关联提问卡后继续具体任务。", "prompt": "查看待处理工作"}
            return {"label": "重新确认方向", "reason": "已在安全节点停下，新补充尚未应用；下一轮会重新判断范围和目标。", "prompt": "根据我刚才补充的要求，先重新判断下一步，不要直接续跑旧任务"}
        if workflow_result_status(response) == "waiting_condition":
            return {"label": "核对待处理条件", "reason": str(response.get("next_action") or "任务停在必要依据或审核缺口处，先核对具体问题再续接。"), "prompt": "查看待处理工作"}
        if workflow_failure_reason(response) or response.get("needs_clarification"):
            return None
        if intent.action == "outline":
            if intent.outline_level == "story":
                return {"label": "查看并确认大纲", "reason": "先审读新走向和伏笔衔接，再决定是否展开细纲。", "prompt": "打开刚生成的大纲，帮我概括第二卷改动和需要我确认的走向；现在先不要生成细纲。"}
            return {"label": "安排近期章节", "reason": "参考设定、大纲与细纲安排实际写作。", "prompt": "参考设定、大纲和细纲，安排接下来三章怎么写。"}
        suggestions: dict[str, tuple[str, str, str]] = {
            "scene_draft": ("查看场景草稿", "候选场景与章节正文隔离，先阅读再决定是否采用。", "查看刚生成的场景草稿，不要改动正史"),
            "story_setting_edit": ("核对设定影响", "设定已保存新版本，先看差异及受影响内容。", "查看刚才的设定差异和受影响范围"),
            "plan": ("查看第一章规划", "先看章节卡，再决定从哪一章写草稿。", "查看当前规划"),
            "write_draft": ("审查这章", "草稿还没有进入正史，先读一遍或交给 Editor。", "审查当前章"),
            "write_review": ("查看审查结果", "Writer 和 Editor 已完成当前版本的交接。", "打开当前审查报告"),
            "review": ("处理审查意见", "根据报告决定修订，或在通过后明确验收。", "按审查意见修改当前章"),
            "revise_draft": ("重新审查", "修订产生了新版本，旧报告不能替代新版本审查。", "重新审查当前章"),
            "revise_review": ("查看新的审查", "新版本已完成复审，可以继续阅读结果。", "打开当前审查报告"),
            "batch_draft": ("查看批次", "草稿批次仍在临时区，先查看逐章结果再决定是否验收。", "查看批次进度"),
            "settings_update": ("继续安排任务", "新设置会从下一次任务开始生效。", "查看当前项目状态"),
            "status": ("选择下一步", "Coordinator 已读完当前状态，可以直接告诉我想推进哪一项。", "先问我下一步建议"),
        }
        item = suggestions.get(intent.action)
        if not item:
            return None
        label, reason, prompt = item
        return {"label": label, "reason": reason, "prompt": prompt}

    def _reuse_current_pass_review(self, project: InkFlowProject, chapter_no: int) -> dict[str, Any] | None:
        """Return a safe handoff when the current draft already has a pass report."""

        report = self.engine.current_pass_review(project, chapter_no)
        return {"step": "engine.review_mode", "result": report} if report is not None else None

    @staticmethod
    def _reuse_workflow_writer(project: InkFlowProject, chapter_no: int, step: str, instruction: str) -> dict[str, Any] | None:
        runtime = active_runtime.get()
        if not runtime or not runtime.task_id:
            return None
        receipt = project.db.get_metadata(f"workflow_writer:{runtime.task_id}:{chapter_no}:{step}")
        if not isinstance(receipt, dict) or receipt.get("instruction_hash") != content_hash(instruction):
            return None
        chapter = project.db.get_chapter(chapter_no)
        if not chapter or chapter["status"] != "draft":
            return None
        later = project.db.get_metadata(f"workflow_writer:{runtime.task_id}:{chapter_no}:revise", {})
        if step == "write" and later.get("version", 0) > receipt["version"]:
            receipt = later
        path = project.resolve_user_path(chapter["path"])
        if (chapter["version"] != receipt["version"] or chapter["content_hash"] != receipt["content_hash"]
                or not path.is_file() or content_hash(path.read_text(encoding="utf-8")) != receipt["content_hash"]):
            raise ValidationGateError("原任务已完成写稿，但当前正文另有变化；保留当前稿，请按当前版本重新核对后续步骤。")
        return {"step": f"writer.{step}", "result": {**receipt, "reused": True,
                "next_action": "复用原任务已写入的同版正文，从尚未完成的审查节点续接。"}}

    async def _conditionally_accept(
        self,
        root: Path,
        chapter_no: int,
        steps: list[dict[str, Any]],
        reviewed: dict[str, Any],
    ) -> dict[str, Any]:
        report = reviewed.get("result", {})
        if report.get("verdict") != "pass":
            steps.append(
                {
                    "step": "memory.accept",
                    "skipped": True,
                    "reason": "Reviewer verdict 不是 pass；没有使用 force。",
                }
            )
            return {"steps": steps, "status": "needs_input", "chapter_no": chapter_no,
                    "reason": report.get("reason") or report.get("stop_reason") or report.get("summary")
                    or "当前审查尚未通过，草稿已保留；请查看审核缺口后继续核对。"}
        steps.append(
            await self._run_step(
                "memory.accept", self.engine.accept_chapter(root, chapter_no, force=False)
            )
        )
        return {"steps": steps}

    @staticmethod
    async def _run_step(label: str, operation: Any) -> dict[str, Any]:
        try:
            return {"step": label, "result": await operation}
        except InkFlowError as exc:
            return {"step": label, "gate": str(exc), "reason": str(exc),
                    "status": "waiting_condition", "error_type": type(exc).__name__,
                    "recovery_node": exc.recovery_node, "recovery_attempts": exc.recovery_attempts,
                    "recovery_exhausted": exc.recovery_exhausted,
                    "next_action": getattr(exc, "next_action", None)
                        or f"先查看 {label} 节点的具体原因，修复对应来源或依赖后从该节点续接；已保存成果保留。"}
