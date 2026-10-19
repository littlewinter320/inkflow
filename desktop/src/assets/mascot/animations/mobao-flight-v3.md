# 墨宝飞行动作 v3

2026-10-15 使用 ImageGen 参考既有墨宝补充过渡动作。原始生成图 `exec-8aa7591e-3ef9-42f0-b087-25d0f7717128.png` 为 1774×887 RGBA，原图及 v2 图集保留。

- 运行图集：`mobao-flight-v3.atlas.png`，2560×1280，8 列×4 行，每格 320×320。
- 播放：12 帧/秒；起飞 0—7 帧，滑行 8—23 帧循环，落定 24—31 帧。起飞及落定各约 0.67 秒。
- 归一：已安装 ffmpeg 确定性裁格、统一约 1.25 倍缩放及透明补边；主体水平中心 160、底部 296，保留姿态变化，不跨格取图。
- 动作切换时长按实际帧数推导。拖动位置直接跟鼠标；减少动态效果时禁用自动飞行及粒子。
- 播放入口统一为 `Mascot.tsx` 的 12 fps；旧素材清单中的采样速率和时长保留，运行时重新计算播放速率，不直接沿用旧值。

已检查图集透明通道、32 个不同画面和锚点，并通过离线宿主行为检查；原生页面的视觉感受另按实际观察记录。

用户录屏 `20261015-0927-00.2355706.mp4` 为约 51.90 秒、2126×1240、30 fps。抽帧显示拖开后长期停在同处；源码曾用 650 毫秒截断实际需 2.5 秒的轻点动作，返回按钮直接归位，闲置计时也只触发一次。本轮改为实际动作结束后收尾、起飞—飞行—落定归位，以及每 18—32 秒才有一次机会的眨眼、观察或短距离往返；连续随机反应避开同款。

位移使用浏览器 `translate3d` 补间，贴图帧时钟不更新位置。录屏是修改前依据，离线检查不代表已经实测新版原生帧率提升。点空白背景可以触发短途互动；输入、正文编辑、按钮和面板拖动区域不作为游戏区域。

滑行按距离取约 240—1400 毫秒，并用自然加减速；起落各 8 帧仍完整播放。点击即时出现有限粒子反馈，随机在 2.5—3 秒的现有短反应间选择，随后按实际动作结束收尾，不排队重复播放。

同日一次 ImageGen 清边编辑仍有红色碎边，且改变了角色尺寸，未替换已校准的 v3。边缘在原生 76 像素显示下的观感仍待观察，不据透明通道存在宣称清边完成。

## 生成提示词

方式：内置 ImageGen，透明背景；参考 v2 飞行动作图，随后由既有 ffmpeg 做确定性图集打包。

```text
Edit this mascot animation sheet into a smoother production sprite sheet with TWICE as many in-between drawings. Preserve the exact same cute white ink mascot, amber eyes, black navy witch hat, orange flame, ink ring body and golden pen, facing RIGHT. Output EXACTLY 32 frames in an EXACT 8 columns by 4 rows grid on a wide 2048x1024 canvas. Each equal square cell has one complete mascot at exactly the same scale and consistent bottom-center anchor, with transparent padding on all four sides. Row 1, frames 1-8: smooth anticipation, tiny squash, then takeoff. Rows 2-3, frames 9-24: a seamless 16-frame gentle flying loop, subtle body flex, eyes looking right, hat flame flowing to the left. Row 4, frames 25-32: smooth braking, gentle settling and neutral pose. Insert genuine intermediate poses with small changes of hat angle, ring flex and flame movement; avoid large pose jumps, inconsistent face sizes and pasted static copies. IMPORTANT clean alpha edges: remove ALL loose red/orange speckles and noise outside the character silhouette; the soft glow belongs tightly to the flame, not scattered background particles. GENUINE transparent alpha, no black/white/checkerboard background pixels, no shadows on a ground, no text, grid lines, borders or scenery. The subject stays in one fixed screen position inside every cell; the app handles translation. This sheet will play at 12 frames per second, so maintain coherent motion and consistent volume across adjacent frames.
```
