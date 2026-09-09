# InkFlow Agent

这里是墨流（InkFlow）的四 Agent 核心：负责理解与调度的 Coordinator，以及 Writer、Reviewer、Memory Keeper 三个小说生产 Agent。确定性的 Python Novel Engine 校验任务单、角色权限、版本门禁与正史事务。

Agent 不进行无边界群聊，而是用带章节、版本、Context Packet 和证据编号的结构化消息协作。Context Builder 将精确正史查询、本地 BM25、可选 BGE-M3/重排、关系扩展和用户偏好编译成每次调用唯一可见的 Context Packet。

完整流程与边界见 [四 Agent 协作与记忆检索](COORDINATION_AND_RAG.md)。

开发安装：

```powershell
..\.venv\Scripts\python.exe -m pip install -e ".[build]"
```

桌面端和 VS Code 扩展只通过本地引擎接口调用此模块；它们各自独立打包、独立更新。
