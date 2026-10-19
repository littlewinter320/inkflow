import { useState } from "react";
import type { VoiceSettings, VoiceStatus } from "./VoiceCenter";
import { readableSourceText } from "./readableNames";

export function VersionRepair({ projectRoot, onVoiceSaved }: { projectRoot: string; onVoiceSaved: (settings: VoiceSettings, status: VoiceStatus) => void }) {
  const [working, setWorking] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const inspect = async () => {
    setWorking(true); setLines(["正在检查当前程序、引擎连接和语音组件…"]);
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const results = await Promise.race([Promise.allSettled([
        window.inkflow.updateStatus(), window.inkflow.request<{ version: string }>("app.initialize"),
        window.inkflow.request<VoiceStatus>("voice.status"), window.inkflow.request<VoiceSettings>("voice.settings.get"),
        projectRoot ? window.inkflow.request("project.status", { project_root: projectRoot }) : Promise.resolve(null),
      ]), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("状态检查没有及时返回。请重新打开墨流后再检查，正文无需重新生成。")), 15000); })]);
      const [desktop, engine, status, settings, project] = results;
      const output: string[] = [];
      if (desktop.status === "fulfilled" && engine.status === "fulfilled") {
        const version = String(desktop.value.currentVersion || "未读取到");
        output.push(`桌面版本：${version}；引擎版本：${engine.value.version}。${version === engine.value.version ? "版本一致，连接正常。" : "版本不一致，请检查应用更新或修复当前安装。"}`);
      } else output.push("程序与引擎的连接检查未完成。请检查应用更新；资源缺失时需要修复当前安装。");
      if (status.status === "fulfilled") {
        setVoice(status.value);
        output.push(status.value.asr?.asr_ready ? "本地识别程序和模型均可用。" : "本地听写组件尚未齐全，可在语音设置查看具体缺项。");
        output.push(status.value.edge?.needs_update ? status.value.edge.compatibility_message || "在线朗读连接组件需要更新。" : status.value.edge?.connection_ready ? "在线朗读连接组件符合当前协议；服务是否可用仍以实际朗读结果为准。" : "在线朗读组件尚未准备好。");
        if (settings.status === "fulfilled") onVoiceSaved(settings.value, status.value);
      } else output.push("语音组件状态未能读取，请重新打开墨流后再检查。");
      if (projectRoot) output.push(project.status === "fulfilled" ? "已重新核对本书当前进度与可恢复状态。" : "本书状态核对未完成；正文未改动。");
      setLines(output);
    } catch (cause) { setLines([readableSourceText(cause instanceof Error ? cause.message : String(cause))]); }
    finally { if (timer !== undefined) clearTimeout(timer); setWorking(false); }
  };
  const updateVoice = async () => {
    if (!await window.inkflow.confirm("更新已安装的 Edge 在线朗读连接组件？只更新连接程序，已有录音和声音档案保留，文件放在本机专用语音目录。")) return;
    setWorking(true);
    try {
      await window.inkflow.request("voice.edge.install", { confirmation: "install_edge_online_voice" });
      setLines(["连接组件已进入后台修复。完成后点击检查，确认状态；已有录音和正文保留。"]);
    } catch (cause) { setLines([readableSourceText(cause instanceof Error ? cause.message : String(cause))]); }
    finally { setWorking(false); }
  };
  return <section className="stack-section"><h3>版本修复</h3><p className="form-hint">检查程序与引擎版本、刷新组件状态，并核对本书进度。发现缺项后只修对应部分；重新生成正文或清空数据不属于版本修复。</p><div className="settings-inline-actions"><button type="button" disabled={working} onClick={() => void inspect()}>{working ? "正在处理…" : "检查并刷新当前状态"}</button><button type="button" disabled={working} onClick={() => void window.inkflow.checkUpdate().then(value => setLines([String(value.message || "版本检查已完成。")])).catch(cause => setLines([String(cause)]))}>检查应用更新</button>{voice?.edge?.needs_update && <button type="button" disabled={working} onClick={() => void updateVoice()}>修复在线朗读连接</button>}</div>{lines.length > 0 && <div role="status">{lines.map((line, index) => <p key={index}>{line}</p>)}</div>}</section>;
}
