# DeepSeek 与 MiMo 缓存策略及观测边界

记录日期：2026-10-15。本文区分官方公开合同、真实请求样本与已接入源码；离线检查不代表真实缓存收益已经验证。研究经 AnySearch 的 `code.doc` 领域发现、检索与官方来源提取；未为研究增加模型请求、预热或基准调用。

## 1. DeepSeek：完整前缀单元复用

依据：[现行缓存指南](https://api-docs.deepseek.com/guides/kv_cache)、[中文指南](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)。

- 默认自动缓存；命中须完整匹配已落盘的缓存前缀单元。落盘位置包括整次请求输入结束、输出结束、跨请求检测出的公共前缀，以及长输入输出中的固定 token 间隔。
- 官方示例 `A+B → A+C → A+D` 中，前两次不命中；识别并落盘公共前缀 A 后，第三次才可命中 A。**消息结束位置不等于请求结束位置**，官方未承诺拆出一条 user 消息就能在首轮单独落盘该段。
- 构建为秒级；不使用后通常数小时至数天清理；尽力而为，不保证命中率。未公布当前固定间隔数值、可配置 TTL 或逐消息落盘控制。
- [历史发布说明](https://api-docs.deepseek.com/news/news0802) 曾写 64 tokens 存储单位；现行指南说明 SWA 下规则已变化，不能把旧数字作为当前 V4 最小粒度保证。
- 实际依据为 `usage.prompt_cache_hit_tokens`、`usage.prompt_cache_miss_tokens`；本地前缀 hash 相同不证明服务端已经命中。

### 模型与别名边界

[当前模型价格页](https://api-docs.deepseek.com/quick_start/pricing)列出 `deepseek-flash` 对应 V4.1-Flash，`deepseek-v4-flash` 与 `deepseek-v4-flash-vision-exp` 为转向它的兼容旧名。不能据别名转发推定缓存必然共享。[4 月公告](https://api-docs.deepseek.com/news/news260424/)写明 `deepseek-chat`、`deepseek-reasoner` 于 7 月 24 日退役，不能继续当作现行独立能力合同。

官方资料存在冲突：[9 月新闻](https://api-docs.deepseek.com/news/news260910)宣布 `deepseek-v4-pro` 转向 Flash，而当前价格页仍单列 V4-Pro-0813。应用保留请求模型与响应模型，不猜实际路由，不自动改用户型号、冻结快照或费用归类。

[思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode)说明，无 `tools` 请求时历史 `reasoning_content` 不加入下一次上下文；不能靠回灌推理内容扩大缓存。

### 已接入的 DeepSeek 策略

1. 保留现有稳定 system/Schema，以及 ContextPacket 的 global、book、canon 前置和章节/请求信息后置。三消息格式可保持，但撤去“独立 user 消息保证首轮稳定段落盘”的解释，不增加消息切块或 64-token 填充。
2. 保留来源版本核验；设定、人物状态、伏笔变化必须进入输入，不能为命中率冻结过期内容。按真实稳定程度排列，不把所有 canon 都视为永远不变。
3. 为现有诊断补充消息布局、思考模式和各资料段 hash/长度，用相邻真实请求定位首个变化段；不保存小说正文到诊断。按端点、请求/响应模型、角色、Schema 分组观察，不推定这些字段就是服务端缓存键。
4. DeepSeek 适配不继承 OpenAI 专属 `prompt_cache_key`。即使用户以 OpenAI 兼容类型配置 DeepSeek 地址，也按 DeepSeek 官方能力处理。
5. 沿既有授权和章节依赖自然连续执行，不新增预热、续命或固定等待，不合并不同角色的职责合同。官方缓存指南并未保证跨角色或跨模型共享命中。

## 2. MiMo：稳定请求前缀与实际 usage

依据：[按量计费](https://mimo.mi.com/docs/en-US/price/pay-as-you-go)、[Chat Completions](https://mimo.mi.com/docs/en-US/api/chat/openai-api)、[Token Plan](https://mimo.mi.com/docs/en-US/price/token-plan)。

- 官方说明请求前缀内容可命中缓存；Chat API 公开 `usage.prompt_tokens_details.cached_tokens`，据实际返回值核算。
- 未公开 `cache_control`、`prompt_cache_key`、最小命中 token、固定粒度或固定 TTL。不能套用 DeepSeek 的缓存单元落盘规则，也不能借其他供应商参数控制 MiMo 缓存。
- Token Plan 的 Flash 输入命中为 2 Credits/token，未命中为 100 Credits/token，输出为 200 Credits/token；这是套餐额度计量，不等于人民币按量价格。
- [工程文章第 2.3/3.1 节](https://mimo.mi.com/docs/en-US/news/latest/updqate)讨论 V2.5 的逐出、缓存亲和及负载均衡；其中命中率与保留时长经验不是当前 V2.6 Token Plan 的服务保证。不能宣称本项目达到 93% 或固定保留数小时。

### 已接入的 MiMo 策略

1. 保持现有 MiMo 请求格式和 SSE 接收方式，不照搬 DeepSeek 三消息边界策略，不添加官方未公开的缓存控制字段。
2. 同角色、同输出合同保持 system 与 Schema 序列化稳定；将真正稳定的书级资料放前面，当前任务、局部证据和变化状态按真实内容更新。继续共用 ContextPacket 和来源门禁，不复制小说引擎。
3. 按端点、请求/响应模型、角色、Schema 观察实际 `cached_tokens`。Token Plan 与按量端点分别统计成本口径，不为可能的缓存收益自动换端点或模型。
4. 用既有任务中的重复必要资料自然复用；不为提高百分比扩大无关上下文，不通过多调一次、固定延迟或额外摘要预热缓存。

## 3. 已观察样本：不能外推整项任务

以下为本轮真实 MiMo 请求记录汇总，未构成优化前后对照；输入与命中数来自可用 usage。未知用量不按零计算。

| 请求 | 输入 tokens | 命中 tokens | 本次输入命中率 | 边界 |
|---|---:|---:|---:|---|
| Reviewer 成功请求 | 37,824 | 37,760 | 99.83% | 仅该次成功请求；前一次断流 usage 未知 |
| 世界设定单条 Memory 请求 | 113,996 | 16,384 | 14.37% | 不代表其他 Memory 任务 |
| Coordinator 请求 | 31,606 | 12,288 | 38.88% | 不代表章节写稿 |
| Writer 断流请求 | 未知 | 未知 | 未知 | 约 72.7 秒断流，不能据此推断命中或实际费用 |

Reviewer 的 99.83% 不代表任务整体、Writer 或整本小说。汇总只能对缓存统计完整的请求计算 token 加权比例，并同时说明未知请求；不能简单平均上述百分比，也不能把断流请求从成本风险中隐去。本文不提供 DeepSeek 的真实收益样本。

## 4. 不可宣称与实施状态

不能宣称固定 80%/93%/99.83% 整体命中、固定 TTL、三消息必然优于两消息、跨别名共享缓存、提高命中必然解决断流或缩短生成总时长。命中只减少输入侧重复计算，输出仍需生成。

源码已通过冻结的 Settings 派生 `prompt_cache_policy`，接入章节、篇章、规划、日常对话、协作回复与手动修改的 ContextPacket。服务商保存后新任务采用对应策略，运行中任务的原接口不改变；不新增独立角色或第二套小说引擎，不覆盖用户保存的容量。

- **DeepSeek**：保留 `system/reference/task` 消息格式，以及批次已有的共享取材和按源章追加事实；资料顺序为书级约定与规划、客观事实、设定、变化状态及当前任务。独立消息不是缓存落盘保证。OpenAI 兼容类型配 DeepSeek 地址也不发送 `prompt_cache_key`。
- **MiMo**：保留 `system/user` 及 SSE；设定按已选择集合与记录的稳定身份排序，当前人物状态与伏笔进度放动态段。选择仍按相关性与权限完成，不通过固定旧状态换取缓存；不照搬 DeepSeek 消息拆分、64-token 对齐或缓存控制字段。
- **共同修正**：设定覆盖、选择及未装入目录移动到动态尾，完整保留其内容。单条设定核对继续装入四份原文；只有标准 BOOK 精确相等、生效三层规划完整核验且数据库执行视图精确等于原文投影时，才用来源元数据代替重复 Brief/PlanBundle。无法证明重复就保留完整结构，不能删除独有内容。此处只减少模型输入副本，不改规划或小说存储。
- **统计隔离**：现有账本保存规范完整接口、请求模型、响应模型、角色、输出合同、实际思考开关及消息布局。当前角色主条按所选接口和该角色实际请求模型计算；旧记录缺完整接口身份时只留历史。完整缓存统计以 token 加权，断流和未发送尝试分别处理。诊断不保存小说原文、凭据或隐藏推理。

新增三个定向离线用例分别检查服务商切换及实际请求、缓存分组身份、重复投影的安全退回，均通过；界面 TypeScript 检查通过。它们未发起付费模型请求，不能证明命中率已提高。第一次采用新编排也可能无法复用旧布局的缓存；后续仅观察原授权正常任务的实际 usage，不额外预热。

用户随后明确的三档策略已经替换旧的调用方思考开关及固定等待。DeepSeek/MiMo的节省档仅Writer、Coordinator关闭思考，其他角色开启；平衡与高质量五角色开启，格式补救维持该选择。等待按[创作策略设计](CREATION_STRATEGY_DESIGN.md)统一计算，实际思考、合同及参数进入独立观察分组；不能把切换前后的不同思考合同混作同范围性能对照。高质量仅取消时间总上限，缓存、Token与权限边界不变。
