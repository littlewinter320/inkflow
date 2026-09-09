# InkFlow Agent

这里是墨流（InkFlow）的独立小说 Agent：Writer、Reviewer、Memory Keeper，以及确定性的 Python 编排器。

开发安装：

```powershell
..\.venv\Scripts\python.exe -m pip install -e ".[build]"
```

桌面端和 VS Code 扩展只通过本地引擎接口调用此模块；它们各自独立打包、独立更新。
