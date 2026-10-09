# VS Code 模型接口与任务工作台改进记录

## DesignContext

目标是把「导入第三方模型 API 配置，并在扩展内检查连接」做成可用入口，同时让小说任务失败后可以检查原因和安全续接。沿用墨宝、VS Code 主题与现有侧栏；主视觉保持当前会话，模型接口在项目区域之外单独折叠，空工作区也可访问。配置输入使用原生 QuickPick、密码输入框与保存预览；诊断使用只读 Markdown 和 OutputChannel，减少重复面板。中文功能文字、窄栏换行、键盘焦点、中文输入法 Enter、减少动态效果和现有资源 CSP 都保留。

使用已读 Designly composition-director / typography-director：只有发送按钮是创作区主要操作，接口、任务、MCP 等次级动作按用途折叠；公开状态先于展开详情，密钥不进入布局。未重画品牌或素材，没有为界面生成图片。

参考官方 [Continue 配置字段](https://docs.continue.dev/reference) 和 [显式文件/选区上下文](https://docs.continue.dev/ide-extensions/chat/context-selection)，采用连接字段预览及用户主动附加参考，不引入通用代码 Agent。VS Code 的 [Webview 指引](https://code.visualstudio.com/api/ux-guidelines/webviews)、[Webview 安全](https://code.visualstudio.com/api/extension-guides/webview)、[Workspace Trust](https://code.visualstudio.com/api/extension-guides/workspace-trust) 和 [OutputChannel API](https://code.visualstudio.com/api/references/vscode-api#OutputChannel) 支持原生配置、受限消息和可复核输出。

## 已改源码与调用契约

| 命令 | 使用入口 | 后端/行为 |
| --- | --- | --- |
| `inkflow.configureApi` | 手动配置 API | `provider.status` 公开预览 → 用户保存确认 → `provider.configure` → `provider.status` 生效复读 |
| `inkflow.importApi` | 导入 JSON/JSONC | Host 解析白名单连接字段，复用同一保存流程，不导入命令与权限 |
| `inkflow.checkConnection` | 检查模型连接 | 提示可能计费并明确触发 `provider.test`，底层最多三次短请求，外层不重试，诊断总超时 240 秒 |
| `inkflow.diagnostics` / `copyDiagnostics` | 查看/复制诊断 | `app.initialize` + `provider.status`，不会发模型请求；只展示公开投影 |
| `inkflow.showOutput` | 运行输出 | 请求编号、方法、耗时与公开阶段，stderr 原文不复制 |
| `inkflow.resumeTask` | 继续未完成任务 | `task.list` 选择 → 重读资格 → 确认；`resume_available` 使用原 conversation_id 的「继续上次任务」，安全 retry 使用 `task.retry` |
| `inkflow.attachFile` / `attachSelection` | 附加参考 | 限项目 Markdown、稳定保存版及 16000 字符，记录路径/行号/哈希，下一条 `conversation.send` 使用 |

实际 RPC 是 `provider.configure`、`provider.test`，没有新增 settings.update/probe 别名或第二套网络客户端。密钥交给现有墨流系统凭据；共享普通配置、角色选模与冻结的旧任务快照仍由后端维护。不同 host 且缺新密钥时要求明确选择；已有环境覆盖保存后显示实际值。配置 JSON 不包含新的角色、调用预算、工具权限或任意 shell。

任务状态依据明确结果、子步骤和工作流结束事件，不依据“完成了”字样；等待选择/条件保持等待状态。失败消息保留错误样式和原因/输出/续接入口。取消验收恢复按钮可用，三轮提问后保留待答而不递归无限续问。删除未使用旧 HTML 副本、修正 JSONC 解析错误未被检测、历史答复重复逐字播放、中文输入法确认文字误发送和跨盘路径边界。

## 验证与待观察

`extension/scripts/check-workbench.cjs` 已通过：纯内存转译，mock VS Code 资源 URI；检查导入字段、无效 JSONC、URL 凭据、Gemini 协议、脱敏、恢复资格、嵌套等待/失败、生成脚本语法、CSP、空工作区入口和 manifest/注册命令对应。静态审查补充了明确非文本字段拒绝、API 工具失败不结束正在运行的聊天、JSON 字符串密钥字段脱敏；新增最少断言后通过并重新封包。最终 `npm run package` 的 TypeScript 检查、esbuild 编译及 VSIX 生成成功。没有安装或真实模型探测，附件哈希与共享引擎身份见 RELEASE_0.7.4.md。

待观察：不同第三方导出结构、同机环境变量覆盖、系统凭据提示、VS Code 窄栏及高对比主题、实际引擎版本不匹配和网络错误分类。这些只读/格式诊断不能冒充真实小说流程验证。YAML 与秘密变量引用不在本次导入协议中，原生 Gemini 不转译，角色模型不因默认模型探测成功被标为已验证。
