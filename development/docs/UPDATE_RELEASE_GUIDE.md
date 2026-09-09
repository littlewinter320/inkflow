# 墨流（InkFlow）更新发布指南

桌面端使用 GitHub Releases 作为公开更新源。0.3.1 起，安装包内已经写入 `littlewinter320/inkflow` 的公开发布地址；用户在“检查更新”中无需填写 GitHub 账号、令牌或下载地址。

## 发布一个新版本

1. 同步 `agent/src/inkflow/__init__.py`、`agent/pyproject.toml` 和 `desktop/package.json` 的版本号；构建脚本会拒绝不一致的版本。VS Code 扩展可独立升版。
2. 创建并推送对应标签，例如 `git tag v0.4.2; git push origin v0.4.2`。
3. GitHub Actions 会在 `v*` 标签上自动构建 Windows 安装包，并把安装包、`latest.yml` 与 `.blockmap` 发布到同一个正式 Release。也可以在已推送标签的本地机器运行 `./development/scripts/build-release.ps1 -Publish`。
4. `latest.yml` 必须和安装包在同一个正式 Release 中；只上传 `.exe` 或只推送代码都不会触发桌面更新。

`latest.yml` 是桌面更新器读取的版本清单，不能只上传 `.exe` 而遗漏它。VSIX 目前也随 Release 提供；若以后发布至 VS Code Marketplace，应额外使用 Marketplace 发布者账号，不能把 GitHub 登录令牌交给客户端。

## 公开仓库的影响

公开仓库会让用户可以匿名下载 Release，这正是大众客户端自动更新需要的条件。源码、Issue 和提交历史也会同时公开；发布前应确认没有 API Key、个人路径、测试小说或不准备公开的素材。

当前安装包没有商业代码签名证书，因此 Windows 仍可能出现未知发布者提示。更新功能与代码签名是两件事：前者已经可用，后者需要单独购买并配置证书。
