# 墨流（InkFlow）更新发布指南

桌面端使用 GitHub Releases 作为公开更新源。0.3.1 起，安装包内已经写入 `littlewinter320/inkflow` 的公开发布地址；用户在“检查更新”中无需填写 GitHub 账号、令牌或下载地址。

## 发布一个新版本

1. 同步 `agent/src/inkflow/__init__.py`、`agent/pyproject.toml` 和 `desktop/package.json` 的版本号；构建脚本会拒绝不一致的版本。VS Code 扩展可独立升版。
2. 创建并推送对应标签，例如 `git tag v0.5.1; git push origin v0.5.1`。
3. GitHub Actions 会在 `v*` 标签上自动构建 Windows 安装包，并把安装包、`latest.yml` 与 `.blockmap` 发布到同一个正式 Release。也可以在已推送标签的本地机器运行 `./development/scripts/build-release.ps1 -Publish`。
4. `latest.yml` 必须和安装包在同一个正式 Release 中；只上传 `.exe` 或只推送代码都不会触发桌面更新。

`latest.yml` 是桌面更新器读取的版本清单，不能只上传 `.exe` 而遗漏它。VSIX 目前也随 Release 提供；若以后发布至 VS Code Marketplace，应额外使用 Marketplace 发布者账号，不能把 GitHub 登录令牌交给客户端。

## 如何减少用户下载量

桌面沿用 electron-updater 的 NSIS 差分下载：比较旧安装包与新安装包的块索引，复用未改变的数据，只下载变化的块。构建显式启用 `differentialPackage`，客户端允许差分下载；本地发布脚本与 CI 要求安装包、`latest.yml`、`.exe.blockmap` 三者齐全。保留旧 Release 的安装包和 blockmap，且更新服务器必须支持 Range 请求。

这减少的是下载量。下载后仍由安装器在退出或重启时完整替换应用，保证桌面与 Python 引擎版本配套；不是运行中热替换几个源码文件。旧安装包缓存缺失、blockmap 不可用或服务器不支持范围请求时，会回退完整下载，节省比例取决于实际改动，不能保证每次只下载几 MB。

语音模型和用户资料放在应用安装目录之外，不随日常代码更新重复下载。切换到 Kokoro 的这次迁移会在新程序首次启动时自动清理应用管理的旧 VITS 和 Qwen 0.6B 模型缓存；新模型仍需用户主动点击下载，旧录音、声音档案与听读结果保留。

更改同一版本的代码不会让已安装客户端自动收到更新；只有将来获准发布更高版本后，在线更新流程才会触发。本次代码修改本身不执行发布或升版。

技术依据：[electron-builder 自动更新文档](https://www.electron.build/auto-update/)、[NSIS 配置](https://www.electron.build/nsis/)。

## 公开仓库的影响

公开仓库会让用户可以匿名下载 Release，这正是大众客户端自动更新需要的条件。源码、Issue 和提交历史也会同时公开；发布前应确认没有 API Key、个人路径、测试小说或不准备公开的素材。

当前安装包没有商业代码签名证书，因此 Windows 仍可能出现未知发布者提示。更新功能与代码签名是两件事：前者已经可用，后者需要单独购买并配置证书。
