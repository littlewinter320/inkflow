import MonacoEditor, { DiffEditor as MonacoDiffEditor, loader } from "@monaco-editor/react";
import type { DiffEditorProps } from "@monaco-editor/react";
import { useLayoutEffect, useRef } from "react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });

export const Editor = MonacoEditor;
export function DiffEditor(props: DiffEditorProps) {
  const instance = useRef<monaco.editor.IStandaloneDiffEditor | null>(null);
  const keepModels = useRef({ original: false, modified: false });
  keepModels.current = { original: Boolean(props.keepCurrentOriginalModel), modified: Boolean(props.keepCurrentModifiedModel) };
  useLayoutEffect(() => () => {
    const models = instance.current?.getModel();
    // Detach before disposing models; the React adapter otherwise disposes
    // them while the diff widget still subscribes to their changes.
    instance.current?.setModel(null);
    if (!keepModels.current.original) models?.original.dispose();
    if (!keepModels.current.modified) models?.modified.dispose();
    instance.current = null;
  }, []);
  return <MonacoDiffEditor {...props} keepCurrentOriginalModel keepCurrentModifiedModel onMount={(editor, api) => {
    instance.current = editor;
    props.onMount?.(editor, api);
  }} />;
}
