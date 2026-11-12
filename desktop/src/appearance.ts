/** 六套完整配色保留旧编号，已保存的外观设置可直接沿用；色值是针对墨流调整的组合。 */
export type AccentPalette = "lime" | "jade" | "blue" | "violet" | "amber" | "rose";
export type PaletteMode = "light" | "dark";
type PaletteColors = { bg: string; panel: string; raised: string; line: string; text: string; muted: string; accent: string; secondary: string; tertiary: string; quaternary: string; quinary: string };
type StudioPalette = { id: AccentPalette; name: string; description: Record<PaletteMode, string>; light: PaletteColors; dark: PaletteColors };

// 背景/表面/边界/文字各有职责，不将装饰用浅黄、冰蓝直接当成正文或按钮文字。
export const studioPalettes: StudioPalette[] = [
  { id: "amber", name: "奶杏书页", description: { light: "红褐 · 蜜黄 · 奶杏 · 橄榄 · 雾蓝", dark: "古金 · 赤陶 · 苔绿 · 烟蓝 · 麦棕" },
    light: { bg: "#F3EADC", panel: "#FFFAF1", raised: "#EFE3D0", line: "#C9B7A2", text: "#352B28", muted: "#726054", accent: "#8D493A", secondary: "#DBC36C", tertiary: "#E7CFAB", quaternary: "#8A9671", quinary: "#8CA7AE" },
    dark: { bg: "#181611", panel: "#232018", raised: "#2F291F", line: "#62523C", text: "#F2E6D0", muted: "#B8A991", accent: "#DAB46A", secondary: "#AC785E", tertiary: "#879376", quaternary: "#779598", quinary: "#BF956E" } },
  { id: "jade", name: "青苔墨绿", description: { light: "墨绿 · 鼠尾草 · 杏金 · 灰蓝 · 藕褐", dark: "青玉 · 旧金 · 湖灰 · 赤陶 · 苔绿" },
    light: { bg: "#E9EFE6", panel: "#F7FAF3", raised: "#DDE8D8", line: "#AABEA7", text: "#243B30", muted: "#536C5C", accent: "#28634B", secondary: "#8BAA7D", tertiary: "#C4AC76", quaternary: "#86A6B0", quinary: "#AB8275" },
    dark: { bg: "#101C19", panel: "#182923", raised: "#233A30", line: "#456351", text: "#E2EDE3", muted: "#A2B8AA", accent: "#77B899", secondary: "#BCA774", tertiary: "#698D96", quaternary: "#A47E73", quinary: "#93A674" } },
  { id: "blue", name: "冰川晴蓝", description: { light: "浓蓝 · 天空蓝 · 薄荷冰 · 沙金 · 雾紫", dark: "星蓝 · 深湖青 · 沙金 · 暮紫 · 暖珊瑚" },
    light: { bg: "#EAF3F7", panel: "#F8FCFD", raised: "#DCECF4", line: "#ABC3D5", text: "#22334B", muted: "#526B83", accent: "#2849BE", secondary: "#91C6E7", tertiary: "#A9DFD9", quaternary: "#CDB580", quinary: "#9D95B7" },
    dark: { bg: "#101A2A", panel: "#17263A", raised: "#22364D", line: "#415E7D", text: "#E2EDF8", muted: "#A4B8CF", accent: "#86ABEC", secondary: "#61A5AF", tertiary: "#B8A474", quaternary: "#8B86AE", quinary: "#B07F77" } },
  { id: "lime", name: "曜石朱砂", description: { light: "朱砂 · 银灰 · 曜黑 · 香槟 · 灰绿", dark: "暗朱砂 · 冷银 · 古铜 · 锈青 · 灰紫" },
    light: { bg: "#E6E8EC", panel: "#F5F6F8", raised: "#D9DDE3", line: "#ABB1BD", text: "#23252C", muted: "#5C626E", accent: "#AE2C3C", secondary: "#737D90", tertiary: "#292D38", quaternary: "#BC986D", quinary: "#7E9B95" },
    dark: { bg: "#131518", panel: "#1D2025", raised: "#2A2F36", line: "#505965", text: "#E9EDF1", muted: "#A8B1BA", accent: "#D77C74", secondary: "#909AA6", tertiary: "#B39A76", quaternary: "#6F9495", quinary: "#8A849E" } },
  { id: "violet", name: "陶土海盐", description: { light: "陶土 · 海盐绿 · 麦金 · 雾蓝 · 鼠尾草", dark: "暖陶 · 海松青 · 古黄铜 · 烟蓝 · 橄榄" },
    light: { bg: "#F1ECE4", panel: "#FFF9EF", raised: "#E8DDD0", line: "#C6B5A1", text: "#3B342F", muted: "#716357", accent: "#914F3A", secondary: "#65A19B", tertiary: "#D7B46E", quaternary: "#73889E", quinary: "#A1AA7B" },
    dark: { bg: "#1E1916", panel: "#2B231E", raised: "#392D25", line: "#685341", text: "#EFE5D7", muted: "#BFA991", accent: "#CF9A78", secondary: "#72A29B", tertiary: "#B49E68", quaternary: "#748AA1", quinary: "#8D9A75" } },
  { id: "rose", name: "深海鎏金", description: { light: "海军蓝 · 孔雀青 · 鎏金 · 银蓝 · 暖石", dark: "古金 · 孔雀青 · 钢蓝 · 赤铜 · 苔灰" },
    light: { bg: "#E9EFF2", panel: "#F8FAF9", raised: "#DEE7EA", line: "#AABFC3", text: "#263D47", muted: "#536E76", accent: "#285C70", secondary: "#438C8B", tertiary: "#C7A35B", quaternary: "#889EB0", quinary: "#C0AAA2" },
    dark: { bg: "#0E1A21", panel: "#162730", raised: "#213842", line: "#3D5C65", text: "#E0EBE9", muted: "#9FB5B7", accent: "#C6AA69", secondary: "#518D8A", tertiary: "#7799AB", quaternary: "#A98A77", quinary: "#959C7D" } },
];

/** 将一套配色接到现有CSS语义变量；所有窗口继续使用原appearance保存契约。 */
export function paletteVariables(id: AccentPalette, mode: PaletteMode): Record<string, string> {
  const palette = studioPalettes.find(item => item.id === id) || studioPalettes[0];
  const c = palette[mode];
  const mix = (color: string, percentage: number, base: string) => `color-mix(in srgb, ${color} ${percentage}%, ${base})`;
  return {
    "--bg": c.bg, "--panel": c.panel, "--panel-2": c.raised, "--panel-3": mix(c.accent, 9, c.raised),
    "--line": c.line, "--line-soft": mix(c.line, 46, c.panel), "--text": c.text, "--muted": c.muted, "--faint": c.muted,
    "--ink": c.text, "--accent": c.accent, "--accent-dim": mix(c.accent, 74, c.line),
    "--accent-soft": mix(c.accent, mode === "light" ? 10 : 16, c.panel), "--on-accent": mode === "light" ? "#FFFFFF" : "#17151B",
    "--theme-secondary": c.secondary, "--theme-cool": c.secondary, "--theme-warm": c.tertiary, "--theme-paper": c.tertiary,
    "--theme-fourth": c.quaternary, "--theme-fifth": c.quinary,
    "--theme-green": mix(c.quaternary, 62, c.accent), "--theme-ink": mix(c.accent, 65, c.text),
    "--red": mode === "light" ? "#A52D40" : "#F3A0AA", "--amber": mode === "light" ? "#855D20" : "#E9C481",
    "--shadow": `0 18px 54px ${mode === "light" ? "#27253520" : "#05050B90"}`,
    "--palette-paper": c.panel, "--palette-secondary": c.secondary, "--palette-accent": c.accent,
  };
}
