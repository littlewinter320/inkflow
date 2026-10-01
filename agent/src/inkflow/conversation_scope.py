"""Independent conversation state; novel authority remains project scoped."""
from contextvars import ContextVar
import re
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
