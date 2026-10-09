# VS Code 扩展模型配置与诊断接口审计

日期：2026-10-10。依据为当前工作树中的 `extension/src/extension.ts`、`extension/src/controlHtml.ts`、`extension/package.json`、`agent/src/inkflow/app_server.py`、`config.py`、`provider.py`。这是扩展补完前的静态调用审计和对接依据，没有读取用户小说、发送模型请求、安装软件或验证实际服务商连接。后续实现与构建证据另行记录。

## 直接复用的后台接口

| RPC | 输入 | 返回 / 行为 |
| --- | --- | --- |
| `app.initialize` | `{}` | 墨流版本、协议号、公开能力；适合用户主动查看引擎诊断，不必每个操作重复启动检查 |
| `provider.status` | `{workspace_root?, provider_kind?, base_url?, model?, role_models?}` | 实际配置、`api_key_configured`、`api_key_storage`、模型能力及角色设置；给出后三类配置字段时可进行本地预校验，不保存、不请求模型 |
| `provider.configure` | `{provider_kind, base_url, model, api_key?, role_models?}`，也支持后台白名单内的其他墨流设置 | 保存共享的非敏感用户设置；非空密钥写入系统凭据；返回 `{configured:true, api_key_configured, api_key_storage, ...已接受的非敏感字段}`，不返回密钥 |
| `provider.capabilities` | `{workspace_root?}` | 当前协议及能力；本地读取 |
| `provider.models` | `{workspace_root?}` | `{models:string[]}`；向实际服务商请求模型清单，不能当成本地状态读取 |
| `provider.test` | `{workspace_root?}`，必要时当前小说的 `project_root` | `{connected, message, model, reply, public_reasoning_summary}`；真实模型请求，仅验证当前生效配置能够接收请求并返回约定 JSON |

不存在 `provider.update` 或 `provider.probe`。不要建立一套扩展自己的网络调用来替代现有 RPC。

`provider.status` 的预览字段仅有 `provider_kind`、`base_url`、`model`、`role_models`，不会临时采用新密钥。`provider.test` 捕获当前配置快照，再调用 Provider；实现没有消费请求中的临时 `base_url`、`api_key` 或 `model_override`。因此正确顺序是：导入并显示非敏感预览 → 本地校验 → 用户明确保存共享配置 → 重新读取实际状态 → 用户点击连接检查。不能用未保存表单调用 `provider.test`，却把结果说成该表单已验证。

连接探针请求 JSON 中的 `status:"ok"`、简短 `reply` 和公开说明，输出上限 160 token、关闭 thinking。单次 HTTP 尝试超时不超过 60 秒，底层 Provider 最多三次尝试，整项检查可能超过 60 秒；扩展不应再套一层自动重测。取消与失败分别显示，失败保留已保存配置，不声称已连接。

## 服务商和导入边界

后台支持 `deepseek`、`openai`、`anthropic`、`gemini`、`openrouter`、`ollama`、`custom`。Anthropic 使用原生 `/v1/messages`，其余使用 OpenAI 兼容的 `/chat/completions`；Gemini 要给兼容地址，不能把任意 Gemini 原生请求格式当作已支持。Ollama 不要求密钥。

接口根地址必须是完整 http/https URL，模型名不可为空。切换 `provider_kind` 不会自动改成该服务商的官方地址或模型；导入需要明确的地址及模型。第三方 JSON 的别名、嵌套、多供应商选择在扩展入口显式适配，不能把整个来源对象直接传入后台，更不能在日志或预览中显示原 JSON 的密钥。

设置保存是墨流的全局用户配置，供桌面端和扩展共用，不是当前小说专属配置。角色模型只支持 Coordinator、Writer、Editor、Reviewer、Memory Keeper；导入默认模型不会清掉既有 `role_models`。默认模型连接成功不代表五角色的独立模型均已验证，不应隐藏已有角色配置。

`Settings.from_env` 的环境配置优先于已保存设置，`INKFLOW_API_KEY` 及服务商密钥环境变量优先于系统凭据。保存后应再次读取 `provider.status`，显示实际生效地址、模型及密钥来源。若它们被环境变量覆盖，说明覆盖情况，不能用保存响应中的字段冒充实际生效配置。

审计发现原 `provider.configure` 先保存密钥，后续字段校验失败可使旧凭据被改。本轮已在共享后端修复：复用同一设置合并校验，在任何凭据保存前验证完整非敏感补丁；非文本密钥拒绝，空值不改旧凭据。无效导入的离线断言确认不会调用凭据写入。系统凭据和设置文件仍是两个存储，文件写失败时扩展重新读取实际状态，不声称完整成功。

## 扩展入口与诊断的真实缺口

- 现有 `requestEngine` 已通过隐藏的子进程和 JSON-RPC stdin 调用 `--once` 引擎。引擎路径优先配置路径、扩展内 sidecar、已安装桌面版，再寻找工作区/源码 Python；可复用，API Key 不得放到进程参数中。
- 已创建 `{log:true}` 的“墨流 InkFlow”输出频道，但变量声明为普通 `OutputChannel`，只追加公开事件摘要。没有固定的“打开输出”命令、请求身份/方法/耗时/退出状态的诊断链；stderr 被全部替换成笼统警告。最少补充脱敏的生命周期和结构化错误，不恢复原始 stderr、参数或模型文本倾倒。
- 现有敏感字段过滤主要识别 `apiKey` 和内部思维字段，不覆盖所有 `api_token`、`password`、`authorization`，也不会清理字符串中的密钥。导入预览及诊断使用公开字段白名单，错误和摘要中的已知密钥也要脱敏；保持不输出内部模型思维链。
- `InkFlowControlProvider.run` 对 RPC 的正常返回统一提示“任务完成”，`enginePresentation` 不提供完成状态；等待回答、等待条件也可能播放完成动作。要按 `task_completion_status` / 实际任务状态显示“完成 / 等待 / 未完成”，不能把正常返回等同目标完成。
- Webview 的结果处理虽然用 `data.error` 切换墨宝动作，却没有把错误标记传给保存的消息对象，消息的错误样式及恢复后的状态可能丢失。
- 当前没有模型 API 配置导入和连接检查命令。新入口应在尚未打开小说时也可用，避免被 `ready:false` 隐藏整个工作区内容后无法配置。
- 现行真实界面是导入的 `buildControlHtml`。`extension.ts` 中另有同名旧 HTML 构造函数，应以真实调用链为准，不将改到未使用模板当成已经改善界面。

## 微软 API 依据与实施约束

`createOutputChannel(name,{log:true})` 返回 `LogOutputChannel`，有 debug/info/warn/error 等等级和日志级别，应沿用此频道，不新增日志服务。日志只记录公开诊断元数据，提供可发现的打开入口。[微软 LogOutputChannel 文档](https://code.visualstudio.com/api/references/vscode-api#LogOutputChannel)

墨流已有共享系统凭据存储，扩展无需默认再保存一份密钥。如果确有扩展独立持久化需求，`context.secrets` 提供加密的 SecretStorage，且不跨机器同步；不能使用 workspaceState/globalState、VS Code settings 或 Webview `setState` 保存密钥。手动密钥输入可使用扩展 Host 的 password 输入框，导入文件在 Host 解析，仅回传公开预览。[微软 SecretStorage 文档](https://code.visualstudio.com/api/references/vscode-api#SecretStorage)

现有 Webview 已限制 `localResourceRoots` 至扩展 media，使用 nonce 脚本及 CSP，消息内容通过 `textContent` 渲染。保留这些保护；新接口只接收白名单命令，Host 校验消息字段及类型。API 请求由引擎处理，不在 Webview 放密钥或放开网络 CSP。界面隐藏/恢复只保存非敏感状态，不能保存导入密钥或原始配置全文。[微软 Webview 文档及安全要求](https://github.com/microsoft/vscode-docs/blob/main/api/extension-guides/webview.md#security)

本审计没有运行连接检查或付费模型调用，没有验证实际 API 的权限、余额或可用模型；完成接入的证据应来自扩展实现、相关离线检查、实际打包内容和用户可见流程。
