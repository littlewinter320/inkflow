# 模型输出额度与上下文调整记录

用户要求移除“墨流固定 16K 输出”等旧版界面限制，按设置、当前角色和实际模型能力计算输出额度。此次变更没有自动放大已保存的任务配置，也没有改变质量门禁、授权范围或有界恢复次数。

`Settings.model_limits(model_override=None)` 仅对官方 `api.deepseek.com` 上已核实的 `deepseek-flash`、兼容名称 `deepseek-v4-flash`／`deepseek-v4-flash-vision-exp` 和 `deepseek-v4-pro` 返回已知限额。2026-10-09 查询的 [DeepSeek 官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/) 写明 1M 上下文；[Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/) 明确输出上限为 384K，即 393,216 tokens。此处上下文使用 1,000,000 tokens 的保守值；未知模型及代理接口不套用上述官方限额。

`Settings.context_budget_for(role, model_override=...)` 在现有统一／角色预算内计算实际上下文，按本次覆盖模型、角色模型、默认模型的既有优先级核对已知能力。`Settings.effective_output_tokens(requested, role=None, model_override=None, input_tokens=0)` 返回步骤请求额度、用户输出上限、角色上下文剩余额度及已核实模型输出上限的较小值。输入已占满额度时，在 HTTP 请求发出前说明具体输入估计和预算，保留已有任务。

OpenAI 兼容和 Anthropic 两条 Provider 路径都按完整消息估计输入，包含规则与 JSON Schema；格式修复重试重新核算增加的输入，不突破原步骤／配置额度。原有 JSON 校验、网络恢复和请求账本保持原流程。

引擎组包时，Writer 按章目标字数的 2.4 倍、至少 8,000 tokens 估计本步骤输出预留，其余审查角色沿用本步 8,000 tokens，再由统一额度计算取小值；这是输入组包预留，不是模型输出上限。例如全局输出设置 200K、Writer 角色上下文 96K、章节目标 5,000 字时，预留 12K，仍有 84K 输入空间，保留用户的 200K 设置。Provider 发出实际请求前仍按完整输入、步骤请求和用户设置重新算额度。

设置保存不再固定限制 128K，仍要求正整数、用户输出不大于已配置的最大上下文，默认模型已知输出能力也必须满足。未知模型只使用可核实的配置预算，不声称模型支持无限额度。`provider.status` 返回模型已知限额、输入扣除前的默认／角色额度；编辑模型和角色模型时可以只读预览，不保存设置或调用模型生成。

桌面设置显示“输出设置”“模型已核实上限”“输入扣除前输出额度”，实际请求还会扣除完整输入。输出及上下文字段根据当前模型能力与配置调整，不保留 128K、512K／1M 的通用固定界面上限；未知模型明确标为未核实。修改配置仍不会扩大已冻结的旧任务额度。

离线检查入口：`agent/scripts/check_dynamic_output_budget.py`。覆盖大于 16K 的输出、官方 393,216 上限、未知模型按配置预算、角色模型／单次覆盖、输入占满时不发 HTTP，以及两种 Provider 使用完整 Schema 输入核算。HTTP 全部模拟，费用账本隔离在 D 盘临时目录。执行结果由本次整体修复记录，不以脚本存在冒充已经验证。
