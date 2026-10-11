# 墨宝 0.7.5 透明素材与动作接入

日期：2026-10-11。本文记录 0.7.5 的素材实作；构建、安装包及发布结果以该版本 Release 记录为准。素材完成不等于已在安装后的应用中实际播放验证。

## 实际交付

用户提供 **8 段原始 MP4**：前五段及“左右移动”“完成文件”“轻点窗口入口”。另以墨宝原始标志作为参考，通过 image_gen 补齐“等待决定”“暂停交接”“展示成果”三组动画，每组 **12 张不同的透明绘制帧**，共 36 张；这些不是把同一张图片重复十二次。

最终使用 11 段 WebM、11 张透明 poster、3 张 4×3 PNG 图集。左右移动按用户本次允许的水平翻转共用一个视频，因此覆盖十二个动作位。旧 waiting/success/rest/reading 名称继续映射到相应新素材，避免业务调用入口失效。

素材目录为 `desktop/src/assets/mascot/animations`，文件统一使用 `mobao-clean-` 前缀。36 张详细帧保存在三个 `mobao-clean-*-frames` 子目录；运行播放器引用三张低负荷图集，不逐张下载这 36 张详细 PNG。完整来源、原文件 SHA256、输出尺寸、时长及字节数见同目录 `mobao-clean.manifest.json`。

最终冻结素材的实际大小如下；不含 JSON、旧版未引用素材或原始 MP4。

| 范围 | 文件数 | 字节数 |
| --- | ---: | ---: |
| 透明 WebM | 11 | 1,680,823 |
| 透明 poster | 11 | 915,341 |
| 4×3 PNG 图集 | 3 | 1,272,121 |
| 本次桌面新运行素材合计 | 25 | 3,868,285 |
| 独立详细帧，供后续编辑 | 36 | 4,138,769 |
| 扩展实际使用的资源 | 13 | 1,511,140 |

最后修正的 `mobao-clean-point-result.webm` 为 146,251 字节，SHA256 为 `9c515a2eeabacf745f02fb658e21e8a7e69cf6561db63222e409e5888582b7ef`；其 poster 的 SHA256 为 `ff85f90f4131dc0e097d0193991538e830c7a1a7095fa8c0217cdf718c74aafa`。这份输出用于最终封包，先前临时预览版本不代表交付素材。

## 去背景方法和来源

本次使用已存在的开发依赖 rembg 2.0.81、ONNX Runtime 1.29.0 和 OpenCV 5.0.0。先用 ISNet anime 生成逐帧语义掩膜，再用 OpenCV GrabCut 的前景/背景种子细化；模型和开发依赖不随墨流客户端打包。

- [rembg 官方仓库](https://github.com/danielgatis/rembg)采用 [MIT 许可证](https://github.com/danielgatis/rembg/blob/main/LICENSE.txt)。其 [模型列表](https://github.com/danielgatis/rembg#available-models)将 `isnet-anime` 的模型来源指向 SkyTNT。
- [SkyTNT/anime-segmentation](https://github.com/SkyTNT/anime-segmentation)采用 [Apache-2.0](https://github.com/SkyTNT/anime-segmentation/blob/main/LICENSE)；[官方模型卡](https://huggingface.co/skytnt/anime-seg/blob/main/README.md)同样标明 Apache-2.0。所用权重为 rembg 官方 GitHub release 中的 `isnet-anime.onnx`，下载 URL 和校验值直接对应其 [官方 session 实现](https://github.com/danielgatis/rembg/blob/main/rembg/sessions/dis_anime.py)。不能用 rembg 本身的 MIT 许可证替代模型许可证。
- [OpenCV 官方 GrabCut 文档](https://docs.opencv.org/4.x/d8/d83/tutorial_py_grabcut.html)说明按掩膜种子迭代分离前景的方法；其源码许可证见 [OpenCV LICENSE](https://github.com/opencv/opencv/blob/4.x/LICENSE)。

模型实际下载到 D 盘专用目录 `D:/墨流/media-models/models/isnet-anime/isnet-anime.onnx`，大小 **176,069,933 字节**，MD5 为 `6f184e756bb3bd901c8849220a83e38e`。首次将已有权重复制到 D 盘旧目录布局未被当前 rembg 识别，初始化随后从官方 GitHub 下载到上述新布局；本次没有安装新的依赖，也没有向 C 盘下载权重。重现脚本在初始化前校验实际文件和官方 MD5，缺少权重时直接停止，避免静默下载到别处。

针对实际输入所作的修整：

1. 火焰变化会让相邻帧的语义置信度变动；严格的统一阈值曾造成欢迎片段六帧白脸缺失。采用较宽的语义种子后由该帧颜色模型细化轮廓，保护白脸及纸页。
2. 清除位于角色下方的分离舞台反光，包括被火焰染成橙色的细条。已检视的底部中性舞台区域作为背景种子；审读片段低处的纸页明确保留。
3. “完成文件”片段的空白背景板与笔尖接近。让主体外侧的低置信度接触区域交由 GrabCut 重估，并清除分离的背景板细线，保留角色的黑笔、金色笔尖和火焰。
4. 一像素边缘使用邻近内部颜色，减轻原舞台背景混入黑帽轮廓的浅色残边；轮廓外添加透明留白，火焰顶端不贴画布边界。

以上舞台区域依据这八段输入校准。用户原视频保留，后续换入不同构图时应重新检查边界，尤其不能将审读纸页当作地面裁掉。

## 动作与位置

所有视频为 384×384、20 fps、VP9 alpha、无音轨。原视频完整动作按所列时长规范化；补充动画每组保留十二张绘制帧，以 4 张绘制帧/秒组成三秒动作，编码帧率为 20 fps。

| 动作位 | 使用位置与作用 | 素材名末尾 | 时长与循环 |
| --- | --- | --- | --- |
| welcome | 欢迎区 | welcome | 3 秒，单次 |
| idle | 对话栏陪伴 | idle | 4 秒，循环 |
| thinking | 理解与安排 | thinking | 3 秒，循环 |
| writing | 实际写作状态 | writing | 4 秒，循环 |
| reviewing | 核对状态及阅读入口 | reviewing | 4 秒，循环 |
| move_right | 窗口内前往结果 | move | 2 秒，循环 |
| move_left | 返回停靠点，水平翻转 | move | 2 秒，循环 |
| point_result | 指向真实完成卡及文件入口 | point-result | 2 秒，单次 |
| tap_result | 轻点只读预览入口 | tap-result | 1.5 秒，单次 |
| show_summary | 展示当前任务实际结果摘要 | show-summary | 3 秒，单次，12 绘制帧 |
| waiting_user | 关联待答卡旁 | waiting-user | 3 秒，循环，12 绘制帧 |
| pause_exhausted | 保留进度并结束本轮尝试 | pause-exhausted | 3 秒，单次，12 绘制帧 |

右向和左向只改变图像方向；位置由窗口内的容器位移控制。减少动态效果时使用当前动作的透明静态图，后台窗口暂停视频与图集计时器。

## 载入和降级

播放器从 `preload="metadata"` 改为仅对当前可见动作使用 `preload="auto"`。poster 先出现，视频实际首帧就绪再替换；只预热当前及常用待机 poster，动作选择器在悬停/聚焦时预热对应图，不在启动时下载所有视频。

快速切换动作取消旧 `play()` 请求时的 AbortError 不再被当作新视频损坏。旧视频迟到的载入、错误和结束事件核对当前元素身份；真实播放失败或四秒仍未就绪时降级。三组生成动作使用 12 帧图集动画兜底，其他视频使用各自静态图。图集在视频载入较慢或失败时才载入；图集同样失败则保持 poster。单次动作在视频、图集和减少动态效果路径均有结束回调，避免等待无法到来的结束事件。

上述代码调整不等于已测得某个启动时间或帧率提升；当前没有浏览器实播性能数据。

## 自动结果引导及收尾

App 只有在能定位本次已完成 run 的真实文件引用时启用 `autoPreview`，复用 `document.read_reference` 在墨流窗口内只读显示成果；无引用时只展示摘要。忙碌、未保存编辑、输入文字、语音、待答或主要弹窗存在时禁用引导，不切走用户正在做的工作。

正常流程为移动 → 两秒指向 → 需要预览时的一点五秒轻点 → 三秒摘要 → 返回 → 退出。自动预览路径约九秒内收尾；无自动预览的摘要路径约七秒。同一 cue 去重，自动只读预览至多一次，手动点击结果按钮也只播放一次轻点后退出。按键、外部点击、滚动、窗口缩放、目标消失或窗口隐藏会清除计时器并结束本次引导。

这里没有控制系统鼠标，也不代替接受正文、发布规划、删除或上传。动画完成不关闭待办或解除审核门禁，业务结果继续由现有引擎、任务状态及公开引用校验。

## 扩展资源

VS Code 的 `controlHtml.ts` 实际引用六段 video+poster，以及一个 reading PNG。本次只替换这 **13 个已使用资源**，继续使用原文件名：idle/thinking/waiting/success/rest/welcome 及 reading；waiting/success/rest 使用相应新生成动作，reading 使用新的审读 poster。`extension/media/mascot/mobao-clean.manifest.json` 逐项记录来源、字节数及 SHA256。没有将全部十一段视频和三十六张帧图重复复制进扩展。

## 检查证据与待观察

- 三个补充动作各十二个 RGBA 帧，逐帧 SHA256 确认每组十二张不同；使用全组共同裁剪和比例，未逐帧改变角色尺寸。
- 对当前 `Mascot.tsx` 提取出的实际函数进行最小离线断言：十二个图集位置不同且有界，AbortError 视为临时取消，自动播放被拒绝按真实失败降级；全部素材导入路径存在。
- 十一段视频全部 **650 个编码帧**均以 `libvpx-vp9` 实际解码成 RGBA；核对帧数、透明外框及每帧存在实心前景。仅看 `alpha_mode` 元数据不能代替此检查。
- 十一个动作各选十二个进度帧在深、浅背景合成并检查；白脸、审读纸页及主火焰保留，白色地面及大背景板已处理。
- 书写原素材强光扫过墨环和笔杆时，细边仍有少量轮廓变化；没有大块白脸或身体缺失。应在最终应用实际播放时继续观察这一处细节。
- 本次浏览器库存为空，创建 in-app browser 返回不可用，所以**没有完成应用或浏览器实播观察**，没有将静态合成当作界面体验验证。本机播放检查页保存在 D 盘素材归档的 `playback-preview/index.html`；临时本机 HTTP 服务已经关闭。
- 桌面类型检查、引导时间轴检查、封包与发布由本次主任务分别记录，不用旧版检查数量替代本版证据。

原视频、生成母版、语义掩膜缓存、转换 manifest 及深浅背景总览保存在 `D:/墨流/media-source/mobao-20261011`。重现入口为 `scripts/prepare_mobao_media.py`；`--mood` 可只重导点名动作，复用按原视频哈希保存的语义掩膜，不重新执行整组模型推理。原始 MP4 不写回、不裁改。
