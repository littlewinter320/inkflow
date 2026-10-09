// Offline only: compile pure helpers into memory and provide a mocked VS Code
// webview. No engine process, network, credential write or model call.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');
function load(name, customRequire = require) {
  const source = fs.readFileSync(path.join(root, 'src', name), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
  const mod = { exports: {} };
  vm.runInNewContext(compiled.outputText, { module: mod, exports: mod.exports, require: customRequire, URL, console }, { filename: name });
  return mod.exports;
}
const helpers = load('workbench.ts');
const config = helpers.apiImportCandidates('{/*source*/"provider":"custom","apiBase":"https://example.test/v1","model":"example-model","apiKey":"secret-value",}')[0];
assert.equal(config.base_url, 'https://example.test/v1');
assert.equal(config.api_key, 'secret-value');
assert.equal(helpers.apiImportCandidates('{"models":[{"provider":"openai","model":"model-a"},{"provider":"custom","model":"model-b","base_url":"https://example.test/v1"}]}').length, 2);
for (const source of ['{"model":"x"}garbage', '{"provider":"custom","model":"x","base_url":"https://u:password@example.test/v1"}', '{"provider":"custom","model":"x","base_url":"https://example.test/v1?api_key=secret"}', '{"provider":"gemini","model":"x","base_url":"https://generativelanguage.googleapis.com/v1beta"}', '{"provider":"openai","model":"x","apiKey":"${ENV_KEY}"}']) assert.throws(() => helpers.apiImportCandidates(source));
const redacted = helpers.redactDiagnostic('Bearer secret-value https://user:pass@example.test/v1?token=abc sk-private ApiKey=another', ['secret-value']);
for (const source of ['{"models":[]}', '{"models":42}', '{"provider":{}}', '{"provider":0}', '{"provider":"openai","model":{}}', '{"provider":"openai","model":42}', '{"provider":"openai","model":"x","base_url":0}', '{"provider":"openai","model":"x","api_key":false}', '{"provider":"openai","model":"x","api_key":null}']) assert.throws(() => helpers.apiImportCandidates(source));
assert.ok(!redacted.includes('secret-value') && !redacted.includes('pass') && !redacted.includes('token=abc') && !redacted.includes('sk-private') && !redacted.includes('another'));
assert.ok(!helpers.redactDiagnostic('{"api_key":"plain-token","authorization":"old-credential"}').includes('plain-token'));
assert.ok(!helpers.redactDiagnostic('{"api_key":"plain-token","authorization":"old-credential"}').includes('old-credential'));
assert.equal(helpers.diagnosticCategory('401 unauthorized'), '鉴权或服务商权限');
assert.equal(helpers.diagnosticCategory('No module named inkflow'), '本地引擎或依赖');
assert.equal(helpers.canResumeTask({ resume_available: true, status: 'failed' }), true);
assert.equal(helpers.canResumeTask({ resume_available: true, status: 'failed', historical: true }), false);
assert.equal(helpers.canRetryTask({ retryable: true, status: 'running' }), false);
assert.equal(helpers.canRetryTask({ retryable: true, status: 'failed' }), true);
assert.equal(helpers.completionStatus({ steps: [{ result: { status: 'waiting_condition' } }] }), 'waiting_condition');
assert.equal(helpers.completionStatus({ status: 'completed', result: { gate: 'source changed' } }), 'failed');
assert.equal(helpers.completionStatus({ questions: [{}] }), 'waiting_user');
assert.equal(helpers.completionStatus({ status: 'interrupted', resumable: true }), 'waiting_condition');
assert.equal(helpers.completionStatus({ reply: '模型自称完成' }), 'unknown');

const mockVscode = { Uri: { joinPath: (_base, ...segments) => segments.join('/') } };
const html = load('controlHtml.ts', (name) => name === 'vscode' ? mockVscode : require(name)).controlHtml({ cspSource: 'vscode-resource:', asWebviewUri: (uri) => ({ toString: () => String(uri) }) }, {}, 10);
const script = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1];
new vm.Script(script); // Parse generated JavaScript, including embedded assets.
const connectionPosition = html.indexOf('id="connectionTitle"');
assert.ok(connectionPosition >= 0 && connectionPosition < html.indexOf('id="workspace"'), 'API tools must remain visible without a novel project');
assert.ok(html.includes("default-src 'none'") && html.includes("script-src 'nonce-"));
assert.ok(script.includes('error:Boolean(data.error)') && script.includes('waiting_condition') && script.includes('!event.isComposing'));
assert.ok(!script.includes('innerHTML') && !script.includes('api_key'));
const extension = fs.readFileSync(path.join(root, 'src/extension.ts'), 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
for (const suffix of ['configureApi', 'importApi', 'checkConnection', 'diagnostics', 'copyDiagnostics', 'showOutput', 'resumeTask', 'attachFile', 'attachSelection']) {
  assert.ok(manifest.contributes.commands.some((entry) => entry.command === 'inkflow.' + suffix));
  assert.ok(extension.includes('registerCommand("inkflow.' + suffix + '"'));
}
assert.ok(extension.includes('CONTROL_COMMANDS.has(record.command)') && extension.includes('assertWorkspaceTrust();'));
assert.ok(extension.includes('"provider.test" ? 240_000') && extension.includes('明确继续使用现有系统凭据'));
assert.ok(!extension.includes('function controlHtml(webview:'));
assert.ok(extension.includes('type: conversationAction ? "result" : "notice"'), 'An unrelated API/diagnostic error cannot clear the running conversation');
console.log('墨流扩展工作台：API 导入/脱敏/状态/恢复资格/生成脚本/命令静态断言通过（全离线）。');
