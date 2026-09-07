# 墨流（InkFlow）更新发布指南

桌面端使用 GitHub Releases 作为公开更新源。0.3.1 起，安装包内已经写入 `littlewinter320/inkflow` 的公开发布地址；用户在“检查更新”中无需填写 GitHub 账号、令牌或下载地址。

## 发布一个新版本

1. 先把 `src/inkflow/__init__.py`、`pyproject.toml`、`desktop/package.json` 和 `vscode-extension/package.json` 的版本号统一提高。
2. 在仓库根目录运行 `./scripts/build-release.ps1 -Publish`。脚本会使用当前电脑已登录的 GitHub 账号创建或更新同版本 Release，并上传 Windows 安装包、`latest.yml`、`.blockmap` 与 VSIX。
3. 打开 GitHub Release，确认不是 Draft，检查安装包与 `latest.yml` 都已存在，再把 Release 发布出去。
4. 用上一版安装的墨流打开“检查更新”验证；发现新版后下载，重启安装。小说项目与 `.inkflow/inkflow.db` 不应受到影响。

`latest.yml` 是桌面更新器读取的版本清单，不能只上传 `.exe` 而遗漏它。VSIX 目前也随 Release 提供；若以后发布至 VS Code Marketplace，应额外使用 Marketplace 发布者账号，不能把 GitHub 登录令牌交给客户端。

## 公开仓库的影响

公开仓库会让用户可以匿名下载 Release，这正是大众客户端自动更新需要的条件。源码、Issue 和提交历史也会同时公开；发布前应确认没有 API Key、个人路径、测试小说或不准备公开的素材。

当前安装包没有商业代码签名证书，因此 Windows 仍可能出现未知发布者提示。更新功能与代码签名是两件事：前者已经可用，后者需要单独购买并配置证书。
