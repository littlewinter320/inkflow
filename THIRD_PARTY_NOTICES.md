# 第三方组件声明

墨流 0.7.0 随安装包包含 [edge-tts](https://github.com/rany2/edge-tts) Python 组件，用于通过网络请求生成语音。该组件的版本约束为 `>=7.2.8,<8`，其原始项目标注 GNU Lesser General Public License v3。许可证文本见随安装包提供的 `edge-tts-LICENSE`，组件源码与维护信息见上游仓库。墨流未宣称与 Microsoft Edge 语音服务存在官方 API 合作关系。

墨宝媒体开发处理使用 [rembg](https://github.com/danielgatis/rembg)（MIT）与 [SkyTNT/anime-segmentation](https://github.com/SkyTNT/anime-segmentation) 的 ISNet 动漫分割模型（Apache-2.0），结合 OpenCV GrabCut 生成透明视频。这些是开发素材处理工具，不是桌面端的运行依赖；模型缓存保留开发机 D 盘，不随安装包或 Git 仓库分发。原始项目及各自许可证以上游仓库为准，实际处理和验证见 `docs/MOBAO_MEDIA_2026-10-11.md`。
