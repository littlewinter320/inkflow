/** 主模块加载失败时仍保留可见提示；此脚本独立于React依赖图，不自动刷新窗口。 */
const bootShell = document.querySelector<HTMLElement>("[data-boot-shell]");
const bootMessage = document.querySelector<HTMLElement>("[data-boot-message]");
const bootRetry = document.querySelector<HTMLButtonElement>("[data-boot-retry]");
// React接管后不覆盖现有页面；仅更新仍然停留在启动壳中的提示。
const showBootFailure = () => {
  if (!bootShell?.isConnected) return;
  if (bootMessage) bootMessage.textContent = "界面资源尚未载入。请检查开发服务或查看启动记录，再选择重新载入。";
  if (bootRetry) bootRetry.hidden = false;
};
bootRetry?.addEventListener("click", () => window.location.reload());
window.addEventListener("error", showBootFailure);
window.addEventListener("unhandledrejection", showBootFailure);
window.setTimeout(showBootFailure, 8000);

export {};
