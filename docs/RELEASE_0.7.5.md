# 墨流 0.7.5 发布记录

当前状态：0.7.5 已于北京时间 **2026-10-11 16:33:45** 正式公开发布，GitHub latest 指向本版；五个附件上传完整且服务端 digest 与本地一致。0.7.4 的标签、附件与验证记录保留。

本版用户可见变化见 [CHANGELOG](../CHANGELOG.md)，逐项改动和已执行的相关检查见 [后续修复记录](FIXES_2026-10-11.md)、[MiMo契约](MIMO_API_2026-10-11.md)、[界面与音频](UI_PROGRESS_PENDING_AUDIO_2026-10-11.md)及[墨宝媒体](MOBAO_MEDIA_2026-10-11.md)。

10月12—13日的后续源码见[实际运行修复](QUALITY_CACHE_2026-10-12.md)和[直接布局与恢复增量](WORKSPACE_AND_RECOVERY_2026-10-13.md)，由PR #4管理，未重建本页所列安装包或挪动标签。本页原有编译、媒体和发布回执不能作为后续增量的运行验证。

## 已完成的编译与包身份

- 引擎使用现有 D 盘环境完成一次 PyInstaller 编译，产物为 21,018,667 字节。隔离目录中的一次 `--once` 初始化返回 0.7.5、墨流（InkFlow），未调用模型。桌面和 VSIX 嵌入引擎 SHA256 均为 `9049894705d1cb80ecffa9fba2418a07f32f316d58de00f1bd1ad3654cda23e2`。
- 桌面 TypeScript、Electron TypeScript 和 Vite 构建通过。首次封包后发现指向动画的背景细边需要修正；素材冻结后只重建 Web 资源并重新封包，未重复编译引擎或重跑全量测试。
- VS Code 扩展 TypeScript/esbuild 编译与 VSIX 封包通过。包内版本和命令检查确认 API 导入、连接检查及任务恢复入口存在。最终指向动画未被扩展引用，因此未重打 VSIX。
- 从最终 `app.asar` 实际提取并逐个核 SHA256：25 个新运行素材合计 3,868,285 字节，全部等于冻结源码素材；指向视频为 `9c515a2eeabacf745f02fb658e21e8a7e69cf6561db63222e409e5888582b7ef`。最终 CSP 包含 `data:` 媒体许可。
- 引导实际 effect 的离线计时器检查通过：正常顺序、完整两秒指向及一点五秒轻点、自动预览至多一次、输入中断清理和减少动态效果收尾；不代替应用实播。
- updater 的版本、安装包名称、大小与 SHA512 一致。安装包和应用的 ProductName 为“墨流 InkFlow”，版本分别为 0.7.5 / 0.7.5.0；`Get-AuthenticodeSignature` 均返回 NotSigned，未宣称有签名发布者。

已上传并核对服务端 digest 的附件：

| 附件 | 字节 | SHA256 |
|---|---:|---|
| InkFlow-Setup-0.7.5.exe | 143,739,229 | `05ab5371653eedafc3eec7b8f935d37f128c0e6445de76e42cad624f649a7fec` |
| InkFlow-Setup-0.7.5.exe.blockmap | 151,441 | `0c1287d3715d67ef374b959f89bb76aaa969a74d2ace908dda78b2b0aa875fb6` |
| latest.yml | 343 | `ac687f24d8089dc3691cdfa6a12906ceefd358b6c5ccc714e07781bab999206b` |
| inkflow-vscode-0.7.5.vsix | 23,463,699 | `b19e00e2456a2aa53950ee71b1ab2b129cf361f4687a463c260132b5bf63918a` |
| SHA256SUMS.txt | 362 | `261f30800894e62ca179f4dad2f025441a08b88847df1b802ffa9707fb72866d` |

`SHA256SUMS.txt` 包含其余四个附件的校验值。五个附件状态均为 uploaded，发布后下载地址均使用 v0.7.5；私人小说、项目数据库、凭据和开发模型缓存没有上传。

## GitHub 发布回执

- 修复提交：`5542dc6e08ab7eb5afa375adbd992cf0832f4878`；[PR #3](https://github.com/littlewinter320/inkflow/pull/3) 于北京时间 2026-10-11 16:31:45 合并。
- 标签对应源码：`add623cfe0bba86c8186c55f4828f0919b362a87`；annotated tag 对象为 `76d93d3001701429dba14b09ecd63cd4bc73ef6e`，远端标签与本地一致。后补的发布回执提交只更新文档，不挪动标签。
- [正式 Release](https://github.com/littlewinter320/inkflow/releases/tag/v0.7.5)：ID `409538895`，draft=false、prerelease=false，当前 latest=v0.7.5。
- 附件 ID：安装包 `630223536`、blockmap `630223493`、VSIX `630223496`、latest.yml `630223498`、校验清单 `630223492`。逐项实际 size 和 digest 见上表。
- [发布工作流](https://github.com/littlewinter320/inkflow/actions/runs/38125237460)：北京时间 16:34:00 完成，conclusion=success。源码检出、已上传附件检测及标签/三个包版本检查成功；因为附件齐全，Python/Node 初始化、引擎构建、依赖安装及桌面发布步骤均 skipped。本次云端未重复构建，不能把这些 skipped 写为又一次编译通过。

## 实际小说来源核验

通过软件本身的来源核验和收尾服务处理一份实际项目，两条已被当前报告接管的旧任务退出活动队列；原始事项16条剩余14条。40章版本、状态和正文hash与事前副本一致。30章已接受、10章草稿保留，没有付费模型请求或人工通过报告。逐项记录留开发机D盘，私人小说、项目路径和模型输入不上传GitHub。

剩余条件包括同版审查的未定位规划引文和未填证据关系、原任务恢复次数耗尽、后续规划复审及两份失效摘要。新生成Schema和入口修复不改旧报告、重置次数或宣布这些事项全部通过。

## 验证边界

本版不执行全量回归、整卷生成、真实 MiMo 账号调用、本地安装或管理员 UAC 验证。当前浏览器不可用，媒体已实际解码及检查深浅背景，应用实播仍待观察。缓存和耗时没有新的同口径供应商前后测量。安装包无代码签名；GitHub 发布状态只按服务端回执补记。
