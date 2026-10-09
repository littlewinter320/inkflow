import { parse, type ParseError } from "jsonc-parser";

export type ApiImport = { provider_kind: string; base_url: string; model: string; api_key?: string };
export const PROVIDERS = ["deepseek", "openai", "anthropic", "gemini", "openrouter", "ollama", "custom"] as const;
export const PROVIDER_BASES: Record<string, string> = {
  deepseek: "https://api.deepseek.com", openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com", gemini: "https://generativelanguage.googleapis.com/v1beta/openai",
  openrouter: "https://openrouter.ai/api/v1", ollama: "http://localhost:11434/v1",
};

export function validateApiConfig(value: ApiImport): ApiImport {
  if (!(PROVIDERS as readonly string[]).includes(value.provider_kind)) throw new Error("不支持此服务商；请选择墨流支持的 API 协议。");
  if (!value.model.trim() || /[\r\n]/.test(value.model)) throw new Error("模型 ID 不能为空或包含换行。");
  let url: URL;
  try { url = new URL(value.base_url); } catch { throw new Error("接口地址需要完整的 http:// 或 https:// URL。"); }
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("接口地址仅支持 HTTP / HTTPS。");
  if (url.username || url.password || url.search || url.hash) throw new Error("接口地址不能包含用户名、密码、查询密钥或片段；请将 API key 单独填写。");
  if (/\/(chat\/completions|messages|generateContent)\/?$/i.test(url.pathname)) throw new Error("请填写 API 基础地址，去掉 chat/completions、messages 或 generateContent 请求路径。");
  if (value.provider_kind === "gemini" && !url.pathname.includes("/openai")) throw new Error("当前 Gemini 接口使用 OpenAI 兼容协议，请填写 /v1beta/openai 基础地址；原生 Gemini 协议不适用。");
  return { ...value, model: value.model.trim(), base_url: value.base_url.trim().replace(/\/+$/, "") };
}

// Imports only connection fields, never prompts, commands, MCP definitions or
// permissions from a third-party file. JSONC errors must not become partial data.
export function apiImportCandidates(source: string): ApiImport[] {
  const errors: ParseError[] = [];
  const parsed: unknown = parse(source, errors, { allowTrailingComma: true });
  if (errors.length || !parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("配置不是有效的 JSON / JSONC 对象；请导出 API 配置后再导入。");
  const root = parsed as Record<string, unknown>;
  if (root.models !== undefined && !Array.isArray(root.models)) throw new Error("models 应为模型连接数组。");
  const values = Array.isArray(root.models) ? root.models : [[root.apiConfiguration, root.settings, root].find((field) => field !== undefined)];
  if (!values.length) throw new Error("配置没有可导入的模型连接。");
  return values.map((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("模型连接条目格式错误。");
    const value = raw as Record<string, unknown>;
    const provider = [value.provider_kind, value.provider, value.apiProvider, "custom"].find((field) => field !== undefined);
    if (typeof provider !== "string") throw new Error("服务商应为字符串。");
    const provider_kind = provider.toLowerCase();
    const base_url = [value.base_url, value.baseUrl, value.apiBase, value.openAiBaseUrl, PROVIDER_BASES[provider_kind], ""].find((field) => field !== undefined);
    const model = [value.model, value.modelId, value.openAiModelId, value.apiModelId, ""].find((field) => field !== undefined);
    if (typeof base_url !== "string" || typeof model !== "string") throw new Error("接口地址与模型 ID 应为字符串。");
    const key = [value.api_key, value.apiKey, value.openAiApiKey].find((field) => field !== undefined);
    if (key !== undefined && typeof key !== "string") throw new Error("API key 应为字符串，不能是其他对象。");
    if (typeof key === "string" && /\$\{|\{\{|^env:/i.test(key)) throw new Error("导入文件引用了环境或秘密变量。请改用手动配置并在密码框填写实际密钥。");
    return validateApiConfig({ provider_kind, base_url, model, ...(key ? { api_key: String(key).trim() } : {}) });
  });
}

export function redactDiagnostic(value: unknown, knownSecrets: Iterable<string> = []): string {
  let text = String(value ?? "");
  for (const secret of knownSecrets) if (secret) text = text.split(secret).join("[密钥已隐藏]");
  return text
    .replace(/https?:\/\/[^\s<>"')]+/gi, (url) => {
      try { const parsed = new URL(url); parsed.username = ""; parsed.password = ""; parsed.search = ""; parsed.hash = ""; return parsed.toString(); } catch { return "[接口地址已隐藏]"; }
    })
    .replace(/\bBearer\s+[^\s,"'}]+/gi, "Bearer [密钥已隐藏]")
    .replace(/\bsk-[A-Za-z0-9_-]+/g, "[密钥已隐藏]")
    .replace(/((?:api[_-]?key|api[_-]?token|authorization|password|secret)["']?\s*[=:]\s*["']?)[^\s,"'}]+/gi, "$1[密钥已隐藏]");
}

export function diagnosticCategory(message: string): string {
  if (/信任|trust/i.test(message)) return "工作区信任";
  if (/ENOENT|No module named|ModuleNotFound|提前退出|引擎路径|Python|依赖/i.test(message)) return "本地引擎或依赖";
  if (/401|403|密钥|unauthorized|authentication/i.test(message)) return "鉴权或服务商权限";
  if (/404|model.*not|模型.*(不存在|名称)|unsupported.*model/i.test(message)) return "接口路径或模型 ID";
  if (/429|rate.limit|额度|余额|quota|502|503|504/i.test(message)) return "服务商限流或服务状态";
  if (/timeout|timed.out|超时|ECONN|ENOTFOUND|网络|DNS|SSL|CERT/i.test(message)) return "网络或连接超时";
  if (/JSON|格式|schema|parse/i.test(message)) return "模型输出格式";
  return "配置或引擎调用";
}

export function canResumeTask(task: Record<string, unknown>): boolean {
  return task.resume_available === true && task.historical !== true
    && ["failed", "cancelled", "interrupted", "waiting_condition", "waiting_user"].includes(String(task.status));
}

export function canRetryTask(task: Record<string, unknown>): boolean {
  return task.retryable === true && task.historical !== true && !canResumeTask(task)
    && ["failed", "interrupted"].includes(String(task.status));
}

export function completionStatus(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "unknown";
  const result = value as Record<string, unknown>;
  const status = String(result.task_completion_status || result.status || "unknown");
  if (result.post_commit_status === "waiting_condition" || result.resumable === true && status === "interrupted") return "waiting_condition";
  if (result.gate || ["failed", "gate_stop", "needs_revision", "interrupted"].includes(status) || result.stop_reason) return "failed";
  if (["unknown", "patch", "replan", "revise", "insufficient_context"].includes(String(result.verdict))) return "waiting_condition";
  if (result.needs_clarification || Array.isArray(result.questions) && result.questions.length || status === "needs_input") return "waiting_user";
  const children = [result.result, ...(Array.isArray(result.steps) ? result.steps : [])].map(completionStatus);
  if (children.includes("failed")) return "failed";
  if (status === "waiting_user" || children.includes("waiting_user")) return "waiting_user";
  if (status === "waiting_condition" || children.includes("waiting_condition")) return "waiting_condition";
  return status;
}
