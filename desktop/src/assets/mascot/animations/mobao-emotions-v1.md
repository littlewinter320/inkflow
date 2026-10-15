# 墨宝动态表情

2026-10-15 使用 ImageGen 参考既有墨宝补充懊恼与生气表情。原始生成图 `exec-196316b6-675d-41b7-a595-6cbe8ca41dfd.png` 为 1774×887 RGBA，原图保留。

- 运行图集：`mobao-emotions-v1.atlas.png`，2560×1280，8 列×4 行，每格 320×320。
- 懊恼使用 0—15 帧，生气使用 16—31 帧；各 16 张画面按 12 fps 循环。
- 通过已安装 ffmpeg 确定性裁格、统一约 1.25 倍比例、主体中心与底部锚点对齐，不脚本改画表情。
- 聊天仅最后一条墨宝回复可动态；运行卡出现或末条为用户发言时，旧回复全部静态。静态头像不挂载帧计时。页面陪伴墨宝单独控制。
- 动态小头像复用统一 Mascot 播放器；写作、思考、校对、等待、完成、休息使用已有对应动态素材，不新增浮动角色或视频播放器。
- 状态切换经两个播放间隔的单一中性姿态衔接，不叠加两张脸；连续飞行动作沿已有过渡帧切换。离屏、页面隐藏、暂停或减少动态效果时停止帧计时；减少动态效果时显示原状态静态图。
- 九组头像使用 `mobao-avatar-atlases.json` 的 128 像素格派生图集，合计 434 帧；每组固定裁切尺寸和同一缩放比例，仅平移定位主体中心及底锚，不逐帧拉伸身体。静态 poster 与该组第一帧像素一致，原来的独立表情图仍保留。
- 完成、休息原属单次动作，只有聊天头像使用往返帧循环以免末帧硬跳；工作台的完整动作继续按原顺序播放一次。派生头像首帧主体约 100—105 像素，中心约 64、底部 118—119，各状态保持相近占格。

素材仍有少量红色碎边，原生小尺寸观感另按实际观察记录，不宣称清边完成。旧素材清单的原采样数据保留，实际播放统一为 12 fps。

## 生成提示词

方式：内置 ImageGen，透明背景；分别以原懊恼、生气头像作为情绪与角色参考，随后由既有 ffmpeg 做确定性图集打包。

```text
Create a polished 2D animated avatar sprite sheet for this EXACT cute MoBao ink mascot, using the two attached emotion portraits. Exact original face, hat, orange flame, golden pen, ink-ring body, warm amber eyes, 3/4 front view. EXACTLY 32 complete frames in a strict 8 columns by 4 rows grid, a wide 2:1 canvas, equal square cells. First TWO rows (frames 1-16): a gentle frustrated/disappointed animation, looks down, eyelids lower, tiny sigh, soft shrug, returns to initial frustrated pose, seamless 16-frame loop. Last TWO rows (frames 17-32): a cute determined annoyed animation, brow lowers, cheeks puff slightly, eyes look aside, tiny head shake, returns to starting annoyed pose, seamless 16-frame loop. Make small true in-between changes in each frame, gradual smooth motion for 12 fps; no sudden large change and no large bobbing or rotation. Keep identical body size, subject center and bottom anchor in every cell with generous padding. No text, no emotion symbols, no loose sparkles, no tears flying off, no speed lines, no red outline or glow noise. Clean solid illustration edges with smooth antialiasing and transparent alpha directly around the silhouette. TRANSPARENT background, no checkerboard painted in, no black/white background, no grid lines, no borders, no ground or shadow. Flame motion should be subtle and coherent. This is a professional production sprite sheet, keep every frame completely inside its own cell and preserve character identity.
```
