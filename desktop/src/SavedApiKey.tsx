import { useEffect, useRef, useState } from "react";

export function SavedApiKey({ provider, revision }: { provider: string; revision: number }) {
  const [value, setValue] = useState<{ preview: string; key?: string; configured: boolean } | null>(null);
  const [error, setError] = useState("");
  const sequence = useRef(0);
  useEffect(() => {
    const id = ++sequence.current;
    setValue(null); setError("");
    void window.inkflow.request<{ api_key_preview?: string; api_key_configured: boolean }>("provider.key", { provider_kind: provider })
      .then(result => { if (sequence.current === id) setValue({ preview: result.api_key_preview || "", configured: result.api_key_configured }); })
      .catch(() => { if (sequence.current === id) setError("密钥状态暂时无法读取"); });
    return () => { sequence.current++; };
  }, [provider, revision]);
  const reveal = async () => {
    if (value?.key) { setValue({ ...value, key: undefined }); return; }
    const id = ++sequence.current;
    try {
      const result = await window.inkflow.request<{ api_key: string | null }>("provider.key", { provider_kind: provider, reveal: true });
      if (sequence.current === id) { setValue(current => current && { ...current, key: result.api_key || undefined }); setError(""); }
    } catch { if (sequence.current === id) setError("暂时无法查看已保存密钥"); }
  };
  return <label className="saved-api-key">已保存的密钥 <small>{error || (value?.configured ? "已保存在本机，重启后继续使用" : value ? "尚未保存" : "读取中…")}</small>
    <span className="saved-api-key-field"><input readOnly aria-label="已保存的API密钥" autoComplete="off" spellCheck={false}
      value={value?.key || value?.preview || ""} placeholder="保存后会显示密钥尾号" />
      <button type="button" onClick={() => void reveal()} disabled={!value?.preview} aria-pressed={Boolean(value?.key)}>{value?.key ? "隐藏" : "查看"}</button></span>
  </label>;
}
