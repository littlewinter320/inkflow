import { Component } from "react";
import type { ReactNode } from "react";

/** 渲染失败时给出可见恢复入口；不自动刷新、不重发任务，也不隐藏故障。 */
export class AppBoundary extends Component<{ children: ReactNode }, { message: string }> {
  state = { message: "" };
  // 只保留简短错误摘要，不将组件栈或书稿送到外部。
  static getDerivedStateFromError(cause: unknown) {
    return { message: cause instanceof Error ? cause.message.slice(0, 240) : "界面组件未能正常显示。" };
  }
  // 错误可见且恢复由作者手动发起，后台任务不在这里重新执行。
  render() {
    if (!this.state.message) return this.props.children;
    return <main className="app-recovery" role="alert"><section><p className="eyebrow">INKFLOW</p><h1>界面暂时没有显示</h1><p>已保存的小说资料仍在本地。重新载入前，请留意尚未保存的界面输入；后台任务状态需在恢复后核对。</p><details><summary>查看错误摘要</summary><p>{this.state.message}</p></details><button type="button" onClick={() => window.location.reload()}>重新载入界面</button></section></main>;
  }
}
