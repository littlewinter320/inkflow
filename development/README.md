# 墨流开发资料

普通用户无需使用本目录。这里集中存放不会直接出现在安装包界面的开发材料：

- `config/`：本地环境变量示例。
- `docs/`：只保留发布操作等当前仍会使用的维护指南；旧计划、测试说明和一次性研究不进入仓库。
- `scripts/`：桌面版与可选扩展的打包脚本。
- `tools/`：图标、墨宝素材等离线处理工具。

正式产品代码仍按 `agent/`、`desktop/`、`extension/` 三个独立目录维护。桌面版与 VS Code 扩展独立升版、独立发布。

## 构建桌面版

```powershell
.\development\scripts\build-release.ps1
```

只有明确需要同时打包 VS Code 扩展时才增加 `-IncludeExtension`。
