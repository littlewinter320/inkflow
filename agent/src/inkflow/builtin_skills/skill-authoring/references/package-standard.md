# 墨流技能包标准

适用：新建、完善、导入或适配运行时技能。这里是产品资源规范，不是AGENTS.md的副本，不给小说Agent源码、shell或外部发布权限。

## 1. Prompt、Skill和Tool如何区分

| 形式 | 适合什么 | 内容 | 例子 |
| --- | --- | --- | --- |
| Prompt | 一次性、几句话能说清的要求 | 目标、输入、范围、输出 | “用三句话解释这条伏笔，不改正文” |
| Skill | 会重复使用、需明确触发和细分交接的方法 | 边界、步骤、角色、输入输出与按需参考 | 有据审核、记忆交接、创建技能 |
| Tool | 需要确定性执行、读写或权限检查的动作 | 程序、参数Schema、版本、错误、回执 | 校验替换范围、读取章节、接受草稿 |

重复频率高不等于要多调用模型；安全或提交检查不能只移进Markdown。模型选中了Skill不等于获准调用Tool。

## 2. 目录和文件

最少包含SKILL.md。简单技能不强造辅助目录。墨流适配元数据在agents/inkflow.json，便于标准入口在其他宿主读取。

- references：步骤真正需要的标准、分支说明、细则和小量教学例子。
- scripts：本宿主只执行已登记工具的JSON调用声明；外部.py/.ps1/.sh等保留查看，不运行。
- assets：用于产物的真实模板或素材。无需每次进入上下文，不把参考说明伪装成素材。
- 复杂技能可提供references/process.md，设置直接排版供作者完整阅读；每步交代输入、操作、输出、验收、展示及失败去向。它不必加入执行适配清单，避免每次重复装入整条流程。实际角色执行需要的规则必须仍写在入口或匹配当步的已声明参考里，不能只放阅读说明。
- 来源与许可随实际复用文件保留。不复制整套仓库或无关资源来凑目录。

## 3. SKILL.md元数据

name必填，1至64位小写字母、数字和单个连字符，匹配目录名。description必填，1至1024字，写清实际能力和什么时候使用。license、compatibility、metadata可以保留；compatibility不超过500字。allowed-tools是外部兼容说明，不是执行许可。

正文建议短于500行。正文不放密钥、完整私有书稿、实时统计、任务ID或不断变化的历史。详尽细则进reference，避免每次改变稳定前缀。

## 4. 入口正文必须说明

1. 名称和具体解决的问题，哪些结果由哪个角色负责。
2. 主动触发方式；被动触发的具体节点、实际角色、前置条件；明确不适用情形和退出条件。
3. 必要输入及其身份：用户原话、当前正文、规划、参考、候选或已接受事实分别标记。
4. 主要任务拆成必要部分；每部分给输入、处理规则、输出、交给谁、失败时保留什么。
5. 如何用清楚中文展示；正式结构引用当次Schema，不自建替代生产协议。
6. 每份reference何时读取，每个脚本调哪个登记工具，每个asset用于哪种产物。
7. 来源不足、错版、许可不足、取消和预算不够时的停止/交接方式。不写无变化的无限重试。

## 5. 每个标准步骤的写法

步骤用“做什么＋依什么判断＋交付什么”，不要只写“认真检查、提高质量”。

- 输入：明确来源、范围、版本和必要字段；不接受虚构的完成回执。
- 操作：确定步骤交Tool；创作步骤说明判断标准和可选方法，不把文学偏好写成固定机械配额。
- 输出：当前Schema中的合法字段或作者可见内容；候选与正式状态分开。
- 检查：说明什么算满足，哪个原程序负责核验；技能说明不代签结果。
- 失败：指出具体缺项、保留已有产物和原预算，从有效节点续接；不靠新增角色或重复整次生成解决。
- 展示：先说实际结果和未完成项，必要时给原句和下一步。代码字段保留原值，公开解释用日常中文。

## 6. 墨流适配清单

schema_version固定1；display_name中文可读；roles只能选coordinator、writer、editor、reviewer、memory_keeper。

activation为manual或automatic。automatic必须用具体contracts限制，填写when和not_when；同名外部技能不会自动覆盖内置技能。contracts描述实际引擎节点，不是模型自由发明的动作。

已登记常用节点：

- writer.draft、writer.scene、writer.plan、writer.outline
- writer.revise、writer.local_revision、writer.selection、writer.length_reduce、writer.length_expand、writer.note
- review.editor、review.reviewer、review.correction、review.claim、review.dispute、review.same_chapter、review.arc
- memory.keeper、memory.repair、memory.evidence
- check.general、check.expression、check.logic_continuity、check.memory（由当前模式分派推导）
- planning.writer、planning.review
- coordinator.route、coordinator.grill、skill.authoring

references每项含path，可按contracts和roles再限制。parameters可声明name、label、required、default；当前输入支持${name}填空，值不会成为工具、路径或角色权限。

steps每项含已登记tool，可引用scripts/*.json；脚本固定为schema_version=1、tool=同一工具、arguments={}。实际输入由宿主当前任务绑定，不从外部包取shell字符串。

## 7. 程序工具与业务工具

技能预处理登记：

- skill.scope：核任务、角色和适用范围。
- skill.resources：核冻结引用与内容哈希。
- skill.output：绑定原输出Schema，真实通过由原校验执行。
- context.inspect：读取当前角色容量，最终输入仍由Provider核算。
- skills.validate：候选文件校验由SkillLibrary.create落实。

章节工具仍是status.read、chapter.read、source.search、plan.preview、chapter.write、chapter.revise、chapter.review_mode、chapter.accept，经原Coordinator授权、范围和版本检查执行。Skill不能在scripts中自动发这些业务操作；Coordinator沿现有流程安排。

## 8. 缓存、成本和恢复

选择技能复用现有Coordinator路由，不另发“选择模型”。列表、预览、开关、校验与文件打开在本地完成。只在明确要求生成或改写技能内容时用一次现有角色请求。

模型依次看到目录、实际当步入口和相关引用；不会每次携带全部库。技能内容稳定排序，时间、任务ID、启用原因和统计放本地回执。更新策略可选：写法与参考文字自动用于新任务、控制变化先确认；或全部修改先确认。角色、触发、工具和流程等控制配置变化必须重新启用；运行及恢复使用原冻结内容。

实际缓存以供应商usage为准；检索命中与采用正确性分别评价。不能把大输入提高命中率视为省钱，不能为了演示节省制造调用。费用未知单列，原模型次数和预算不重置。

## 9. 保存、启用和交付

新包保存为未启用候选，重名不覆盖。作者查看全部文件、触发边界和工具后启用当前内容版本。文件变化按所选更新策略处理，只影响新任务。个人文件可共享，本书开关独立，可恢复继承默认。公开交付应给目录、说明、实际校验问题与未验证项，不声称做过未运行的模型或界面验证。

## 10. 可执行触发与闭环

被动加载先核enabled、automatic、现有角色和contracts；可再声明keywords（作者命令部分至少命中一个）与exclude_keywords（任一命中就排除）。when/not_when写清语义边界供协调与责任角色理解，不能代替可执行的角色、步骤和工具门禁。被引用的小说或文件指令不触发新技能。

每次真实请求绑定技能ID、内容哈希、步骤合同与计费attempt；流程另留实际依赖图及执行回执。只装入或返回不能评为业务成功。沿本书workflow-experience.sqlite保存结果和作者反馈；改版不沿用旧版分数，恢复不重新选版。

自动学习只辅助选择，不自动改技能。明确作者差评或多个独立任务重复失败形成有来源的改进建议，先核根因；作者明确提出改进时才用技能创建请求生成未启用副本及差异。不得为后台优化自动试跑。文风与效果留本书，通用工具故障和真实调用成本只共享短投影、来源与适用条件。

闭环须覆盖进入条件、真实产物、程序验收、失败交接、取消恢复、结果记录及版本迭代。各环节是否发生以对应回执为准：装入不等于已调用，模型返回不等于业务通过，审核通过不等于正式接受，形成改进建议不等于技能已经更新。只审查和只给候选时不能越过相应结束点；旧版本任务不因新技能启用重新开始。

