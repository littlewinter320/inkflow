"""Independent conversation state; novel authority remains project scoped."""
from contextvars import ContextVar
from contextlib import closing
from datetime import datetime, timezone
import json
import re
import sqlite3
from uuid import uuid4
from .errors import ProjectError

active_conversation: ContextVar[str] = ContextVar("inkflow_conversation", default="main")

def conversation_id(value=None):
    value = "main" if value is None else value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ProjectError("对话编号无效。")
    return value

def conversation_key(key):
    current = active_conversation.get()
    return key if current == "main" else f"conversation:{current}:{key}"


def list_conversations(project):
    """列出本小说显式创建的聊天；不把章节接收等内部隔离身份暴露为作者聊天。"""
    with project.db.connect() as connection:
        rows = connection.execute(
            "SELECT value_json FROM metadata WHERE key GLOB 'conversation:*:conversation_session' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        deleted = {row[0].removeprefix("conversation_deleted:") for row in connection.execute(
            "SELECT key FROM metadata WHERE key GLOB 'conversation_deleted:*'")}
    items = []
    for row in rows:
        item = json.loads(row["value_json"])
        # 目录损坏须明确报错，不能用空列表伪装历史已消失。
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not isinstance(item.get("id"), str):
            raise ProjectError("聊天目录记录不完整，原记录已保留，请先核对本地资料。")
        conversation_id(item.get("id"))
        if item["id"] not in deleted:
            items.append(item)
    for item in items:
        for flag in ("pinned",):
            if flag in item and not isinstance(item[flag], bool):
                raise ProjectError("聊天分类记录不完整，原记录已保留，请先核对本地资料。")
            item.setdefault(flag, False)
        if item.pop("archived", False):
            item["pinned"] = False  # 旧归档回到普通列表，不自动删除或迁移作者资料。
    if not any(item["id"] == "main" for item in items) and "main" not in deleted:
        items.append({"id": "main", "title": "原有聊天", "created_at": "", "pinned": False})
    return {"items": items}


def create_conversation(project):
    """只新增独立聊天身份，不复制旧对话、任务、待答或小说内容。"""
    identity = f"chat-{uuid4().hex}"
    item = {"id": identity, "title": "新聊天", "created_at": datetime.now(timezone.utc).isoformat(),
            "pinned": False, "title_initialized": False}
    project.db.set_metadata(f"conversation:{identity}:conversation_session", item)
    return item


def update_conversation(project, identity, changes, *, first_message=False):
    """仅改聊天名称和置顶状态；删除使用独立的显式入口。"""
    if not isinstance(identity, str):
        raise ProjectError("请选择要管理的聊天。")
    identity = conversation_id(identity)
    ensure_conversation_available(project, identity)
    if not isinstance(changes, dict) or not changes or set(changes) - {"title", "pinned"}:
        raise ProjectError("只能修改聊天名称或置顶状态。")
    patch = dict(changes)
    if "title" in patch:
        if not isinstance(patch["title"], str) or not 1 <= len(patch["title"].strip()) <= 80:
            raise ProjectError("聊天名称需为1至80个字符。")
        patch["title"] = " ".join(patch["title"].split())
        if not patch["title"]:
            raise ProjectError("聊天名称不能为空。")
    for flag in ("pinned",):
        if flag in patch and not isinstance(patch[flag], bool):
            raise ProjectError("聊天分类状态无效。")
    key = f"conversation:{identity}:conversation_session"
    with project.db.connect() as connection:
        # 与首句命名共用短事务，防止重命名与任务首条回执互相覆盖。
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM metadata WHERE key=?", (f"conversation_deleted:{identity}",)).fetchone():
            raise ProjectError("这份聊天已删除，名称与置顶没有重新写回。")
        row = connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
        item = json.loads(row["value_json"]) if row else None
        if item is None and identity == "main":
            item = {"id": "main", "title": "原有聊天", "created_at": ""}
        if not isinstance(item, dict) or item.get("id") != identity or not isinstance(item.get("title"), str):
            raise ProjectError("这份聊天不在本书目录中，原记录已保留。")
        if first_message and (item.get("title_initialized") or item["title"] != "新聊天"):
            return item
        item.pop("archived", None)
        item = {"pinned": False, **item, **patch}
        if "title" in patch:
            item["title_initialized"] = True
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            (key, json.dumps(item, ensure_ascii=False), item["updated_at"]),
        )
        connection.commit()
    return item


def ensure_conversation_available(project, identity=None):
    """内部删除标记只阻止旧界面/迟到写回，不保留聊天内容，也不提供恢复列表。"""
    identity = conversation_id(identity if identity is not None else active_conversation.get())
    if project.db.get_metadata(f"conversation_deleted:{identity}") is not None:
        raise ProjectError("这份聊天已删除，请打开本书现有聊天或新建聊天；原任务审计与小说资料仍保留。")


def delete_conversation(project, identity):
    """删除精确聊天文件及会话状态；保留小说、作者偏好、运行审计与累计费用。"""
    from .project_lock import project_write_lock_sync
    from .utils import atomic_write_bytes
    if not isinstance(identity, str):
        raise ProjectError("请选择要删除的聊天。")
    identity = conversation_id(identity)
    path = project.root / "DIALOGUE.md" if identity == "main" else project.internal / "conversations" / f"{identity}.md"
    # 只删除已知的单文件；拒绝链接越界，不遍历或删除任何书稿目录。
    if not path.resolve().is_relative_to(project.root.resolve()) or path.is_symlink():
        raise ProjectError("聊天文件的位置异常，未删除任何文件，请先核对本地路径。")
    with project_write_lock_sync(project.root):
        if project.db.get_metadata(f"conversation_deleted:{identity}") is not None:
            return {"deleted": True, "id": identity}
        if not any(item["id"] == identity for item in list_conversations(project)["items"]):
            raise ProjectError("这份聊天不在本书目录中，未删除其他资料。")
        studio_path = project.internal / "studio.db"
        if studio_path.is_file():
            with closing(sqlite3.connect(studio_path.resolve().as_uri() + "?mode=ro", uri=True)) as studio:
                for raw, in studio.execute("SELECT params_json FROM task_runs WHERE status='running'"):
                    params = json.loads(raw)
                    if (params.get("conversation_id") or "main") == identity:
                        raise ProjectError("这份聊天还有运行中的任务，请先停止任务并等待收尾，再删除聊天。")
        original = path.read_bytes() if path.is_file() else None
        removed = False
        try:
            with project.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM metadata WHERE key GLOB ?", (f"conversation:{identity}:*",))
                if identity == "main":
                    # 原聊天的业务记忆与费用授权共享metadata，不能用清空整表替代删除聊天。
                    for key in ("pending_terminal_question", "pending_terminal_user_updates", "pending_creation_task",
                                "next_step_suggestions", "next_task_settings", "dialogue_turns_since_save"):
                        connection.execute("DELETE FROM metadata WHERE key=?", (key,))
                connection.execute("INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?)",
                    (f"conversation_deleted:{identity}", json.dumps({"id": identity}), datetime.now(timezone.utc).isoformat()))
                if original is not None:
                    path.unlink(); removed = True
                connection.commit()
        except BaseException:
            # SQLite失败会回滚；文件已删时同步恢复原字节，不能只恢复目录而丢掉记录。
            if removed:
                atomic_write_bytes(path, original)
            raise
    return {"deleted": True, "id": identity}


def remember_conversation_title(project, message):
    """以首条作者原话命名；分类和手动名称保留，不额外请求模型。"""
    identity = active_conversation.get()
    title = " ".join(message.split())[:36]
    if identity != "main" and title:
        # 兼容既有内部隔离身份：不把未登记的工具会话误建为作者聊天或阻断原请求。
        if not isinstance(project.db.get_metadata(f"conversation:{identity}:conversation_session"), dict):
            return
        update_conversation(project, identity, {"title": title}, first_message=True)


def conversation_pending_work(items):
    """聊天仅自动携带自己的任务意图；无聊天归属的真实项目维护事项仍保留。"""
    current = conversation_key("pending_creation_task")
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        origin = source.get("conversation") if isinstance(source, dict) else None
        if origin is None or origin == current:
            result.append(item)
    return result


def conversation_collaboration_messages(project):
    """按运行来源隔离聊天提示；工程工作台仍保留全项目协作记录，不修改原始消息。"""
    messages = project.db.list_collaboration_messages(active_only=True, limit=500)
    run_ids = list({item["run_id"] for item in messages if item.get("run_id")})
    owners = {}
    path = project.root / ".inkflow" / "studio.db"
    if run_ids and path.is_file():
        # 只读来源关联，不初始化Studio、不恢复任务；缺失归属不能冒充当前新聊天。
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = connection.execute("SELECT run_id,params_json FROM task_runs WHERE run_id IN ("
                + ",".join("?" for _ in run_ids) + ")", run_ids).fetchall()
        for run_id, raw in rows:
            params = json.loads(raw)
            if isinstance(params, dict):
                owners[run_id] = params.get("conversation_id") or "main"
    current = active_conversation.get()
    return [item for item in messages if owners.get(item.get("run_id"), "main" if not item.get("run_id") else None) == current][:8]
