"""技能本地管理与明确的自然语言创作入口；不新增小说生产路径。"""
from __future__ import annotations

import importlib
import json
import re
from typing import Any

from pydantic import Field

from .errors import ValidationGateError
from .schemas import StrictModel
from .skills import SkillLibrary, public_skill
from .skill_runtime import compose_skill_prompt, _instruction_text


class SkillFile(StrictModel):
    """模型只返回包内文字文件，路径与内容还需由共享库校验。"""
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=100_000)


class SkillDraft(StrictModel):
    """明确创建技能时的候选合同，不是可以任意写磁盘的命令。"""
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    summary: str = Field(min_length=1, max_length=1200)
    files: list[SkillFile] = Field(min_length=1, max_length=40)


def prompt_inventory() -> dict[str, Any]:
    """设置中展示全部集中式提示词及仍保留的旧合同，不把旧恢复文本冒充当前发送。"""
    from .prompts import skill_prompt_core
    items = []
    modules = ("prompts", "terminal_session", "planning_pipeline", "review_rubric", "craft", "humanizer", "grilling")
    seen = {}
    for name in modules:
        module = importlib.import_module("inkflow." + name)
        for symbol, text in vars(module).items():
            if (not isinstance(text, str) or len(text) < 40 or not symbol.lstrip("_").isupper()
                    or not any(word in symbol for word in ("SYSTEM", "CONTRACT", "PERSONA", "PROMPT", "LANGUAGE", "POLICY", "HANDOFF", "PREFLIGHT"))):
                continue
            identity = f"{name}.{symbol}"
            if text in seen:
                seen[text]["aliases"].append(identity)
                continue
            core, contract = skill_prompt_core(text)
            item = {"id": identity, "module": name, "symbol": symbol, "aliases": [],
                "kind": "短应用合同＋当步技能" if contract else "提示词或共享约束",
                "contract": contract, "current_core": core, "legacy_text": text if contract else "",
                "source": "agent/src/inkflow/" + name + ".py", "characters": len(text),
                "note": "旧任务保留原文；新任务只按步骤装入技能。" if contract else
                    "这是集中定义；具体请求还会包含当次任务、材料与Schema，未表示每次全部发送。"}
            items.append(item)
            seen[text] = item
    from pathlib import Path
    catalog_path = Path(__file__).with_name("prompt_catalog.json")
    if catalog_path.is_file():
        catalogue = json.loads(catalog_path.read_text(encoding="utf-8"))
        for entry in catalogue["entries"]:
            items.append({"id": "call:" + entry["id"], "module": entry["module"], "symbol": entry["parameter"],
                "aliases": [], "kind": "调用处系统模板" if entry["parameter"] == "system_prompt" else "任务与资料模板",
                "contract": None, "current_core": entry["literal"] if entry["literal"] is not None else entry["expression"],
                "legacy_text": "", "source": entry["source"], "line": entry["line"],
                "characters": len(entry["expression"]),
                "note": "显示真实调用处的固定文本或构造表达式；动态变量要在本次任务中填入，索引本身不调用模型。"})
    return {"prompts": items, "note": "包括集中定义、旧恢复合同和源码中所有system_prompt/user_prompt调用参数；当次已装技能和引用随原执行过程留痕。"}


def _find_skill(message: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """管理允许选未启用技能；重名要求带库名，不猜作者想改哪一个。"""
    matches = []
    mentions = re.findall(r"@((?:(?:builtin|personal|book)/)?[a-z0-9]+(?:-[a-z0-9]+)*)", message, re.I)
    for item in items:
        names = {item["id"], item["name"], item["display_name"]}
        if mentions:
            if any(token.lower() in {item["id"].lower(), item["name"].lower()} for token in mentions):
                matches.append(item)
        elif any(name.lower() in message.lower() for name in names):
            matches.append(item)
    if len(matches) != 1:
        raise ValidationGateError("请指定唯一技能；重名时使用@personal/名字、@book/名字或@builtin/名字。")
    return matches[0]


async def handle_skill_command(project: Any, message: str, provider: Any) -> dict[str, Any] | None:
    """仅处理明确的技能管理原话，普通@使用交原任务；语音与文本共用此入口。"""
    text = _instruction_text(message).strip()
    if not re.match(r"^(?:(?:请|帮我|给我|我想|我要|麻烦)\s*)?(?:创建|新建|生成|制作|编写|改进|优化|迭代|列出|查看|看看|显示|打开|启用|禁用|停用|关闭|开启|导入|适配)", text):
        return None
    if not re.search(r"技能|\bskills?\b", text, re.I):
        return None
    library = SkillLibrary(project.root)
    inventory = library.list(full=True)
    prefix = re.sub(r"^(?:请|帮我|给我|我想|我要|麻烦)\s*", "", text)
    if (re.match(r"(?:查看|看看|显示|列出)", prefix)
            and re.search(r"学习|经验|反馈|改进建议|迭代建议", prefix)):
        # 经验始终自动记录；作者主动查时复用原本书库，不恢复手动评分页面，
        # 也不为解释学习情况另调模型。带@时仅查看明确指定技能的真实样本。
        from .skill_learning import skill_learning_overview
        identity = _find_skill(text, inventory["skills"])["id"] if "@" in text else None
        overview = skill_learning_overview(project)
        samples = [item for item in overview["samples"]
            if identity is None or any(skill["id"] == identity for skill in item["skills"])]
        proposals = [item for item in overview["proposals"]
            if (identity is None or item.get("skill_id") == identity) and item.get("status") == "suggested"]
        lines = ["技能经验一直自动记录和参考；本次只是读取，不会执行任务、改写技能或调用模型。"]
        if samples:
            labels = {1: "程序目标已完成", 0: "待核、等待或结果未定", -1: "记录了未完成或问题反馈"}
            for item in samples[:5]:
                names = "、".join(dict.fromkeys(skill["id"] for skill in item["skills"]))
                source = "作者明确反馈" if item.get("feedback_source") == "author" else "程序回执，不能等同创作满意度"
                lines.append(f"{names}：{labels[item['reward']]}（{source}）。")
        else:
            lines.append("当前没有符合范围的真实技能样本；不会用演示数据补齐。")
        if proposals:
            for item in proposals[:5]:
                lines.append(f"{item['skill_id']}：{item['reason']} 这是待调查建议，尚未生成或启用改版。")
            lines.append("可明确说“改进 @库/技能名，生成未启用候选”；原包和运行中的版本保留。")
        else:
            lines.append("当前没有满足条件的有效改进建议；一次待核或少量样本不会自动认定技能有错。")
        return {"status": "completed", "reply": "\n\n".join(lines), "skills_managed": True,
            "suggested_prompts": [], "skill_learning_read": {"skill_id": identity,
                "available_samples": len(samples), "suggested_improvements": len(proposals)}}
    if re.match(r"(?:列出|查看|看看|显示).{0,15}(?:全部|所有|我的)?\s*(?:技能|skills?)", prefix, re.I):
        lines = [f"{item['display_name']}（@{item['id']}）：{item['description']}"
            + (" 已启用。" if item["enabled"] else " 未启用。") for item in inventory["skills"]]
        return {"status": "completed", "reply": "当前技能如下：\n\n" + "\n\n".join(lines),
            "skills_managed": True, "suggested_prompts": []}
    if re.match(r"打开", prefix) and re.search(r"目录|文件夹|文件位置", prefix):
        scope = "book" if re.search(r"本书|当前小说|当前书", prefix) else "personal"
        try:
            item = _find_skill(message, inventory["skills"])
            directory = item["directory"]
        except ValidationGateError:
            if "@" in text:
                raise
            directory = library.directory(scope)
        return {"status": "completed", "reply": "技能目录已准备好，可以在外部编辑文件；改动按设置中的更新方式用于新任务，流程和工具变化仍需查看后启用。",
            "skill_directory": directory, "skills_managed": True, "suggested_prompts": []}
    if re.match(r"(?:启用|禁用|停用|关闭|开启)", prefix):
        item = _find_skill(message, inventory["skills"])
        enabled = not bool(re.match(r"(?:禁用|停用|关闭)", prefix))
        passive = bool(re.search(r"被动|自动触发", text)) if enabled else False
        control = "library" if re.search(r"个人默认|所有(?:小说|书)|全局", text) else "book"
        updated = library.configure(item["id"], enabled=enabled, automatic=passive,
            expected_hash=item["hash"], control_scope=control)
        return {"status": "completed", "reply": f"{item['display_name']}已{'启用' if enabled else '关闭'}"
            + ("，允许在适用步骤被动触发。" if enabled and passive else "，使用@技能名主动调用。" if enabled else "。")
            + ("这次只改本书开关。" if control == "book" else "这次修改个人默认，已有本书覆盖继续生效。")
            + "正在执行的任务仍沿原技能版本。", "skill": updated, "skills_managed": True, "suggested_prompts": []}
    if re.match(r"导入", prefix):
        # 路径由作者明确给出；没有路径时交设置文件选择器，不让模型猜磁盘位置。
        path = re.search(r"(?:从|路径[：:]?)\s*[\"“]?([A-Za-z]:[\\/].+?)[\"”]?(?:[。\n]|$)", message)
        if path:
            source = path.group(1).strip().rstrip('”"')
            preview = library.preview(source)
            scope = "book" if re.search(r"本书|当前小说", text) else "personal"
            saved = library.import_skill(source=source, scope=scope, expected_hash=preview["hash"])
            return {"status": "completed", "reply": "外部技能已保存为未启用候选。请在设置的技能详情查看全部文件、兼容提醒与触发边界，再启用当前版本。",
                "skill": saved, "skills_managed": True, "suggested_prompts": []}
        return {"status": "waiting_user", "reply": "请在设置 → 技能与提示词中选择技能目录或ZIP；也可以说“从 路径 导入技能”。导入后先作为未启用候选供你查看。",
            "skills_managed": True, "suggested_prompts": []}
    if re.match(r"(?:创建|新建|生成|制作|编写|适配|改进|优化|迭代)", prefix):
        if re.search(r"(?:不要|禁止|无需).{0,10}(?:模型|调用|付费)", message):
            return {"status": "waiting_user", "reply": "这次要求不调用模型。可以在设置 → 技能与提示词中新建简单模板，再打开目录填写；这里未发送生成请求。", "skills_managed": True, "suggested_prompts": []}
        scope = "book" if re.search(r"本书专用|只(?:在|对|给).{0,8}(?:本书|当前小说)|放在本书", text) else "personal"
        original = _find_skill(message, inventory["skills"]) if re.match(r"(?:改进|优化|迭代)", prefix) else None
        evidence, outcome_examples = [], []
        if original:
            from .skill_learning import skill_learning_overview
            overview = skill_learning_overview(project)
            evidence = [item for item in (overview["proposals"] if overview["enabled"] else [])
                if item["skill_id"] == original["id"] and item["skill_hash"] == original["hash"]]
            source_ids = {identity for item in evidence for identity in item["sample_ids"]}
            outcome_examples = [{key: sample.get(key) for key in ("id", "action", "result", "feedback", "selection")}
                for sample in overview["samples"] if sample["id"] in source_ids][:6]
            # 改进采用本书效果证据时先留本书；明确要求个人通用才跨书保存候选。
            scope = "personal" if re.search(r"个人(?:库|通用)|跨书(?:共享|使用)|所有(?:书|小说)", text) else "book"
        system = compose_skill_prompt("你是墨流Coordinator。本次只按作者要求生成一个技能候选包，返回SkillDraft。"
            "不创作小说、不写源码、不执行外部程序。角色、工具和输出Schema以墨流当前登记为准；"
            "SKILL.md包含name/description，agents/inkflow.json使用协议1。简单技能不强造辅助目录。"
            "只输出必要文件，脚本只允许登记工具JSON；新包保存后默认未启用。", "skill.authoring")
        result = await provider.generate_json(system_prompt=system,
            user_prompt=json.dumps({"author_request": message, "scope": scope,
                "existing_names": [item["name"] for item in inventory["skills"]],
                "tools": inventory["tools"], "chapter_tools": inventory["chapter_tools"],
                "original_skill": {key: original[key] for key in ("id", "hash", "texts")} if original else None,
                "improvement_evidence": evidence,
                "outcome_examples": outcome_examples,
                "revision_policy": "保留原包；生成不同名的未启用候选，说明修改理由与影响。来源不足时不宣称已修好。"}, ensure_ascii=False), output_model=SkillDraft,
            agent_role="coordinator", thinking=False, effort="low", max_tokens=8000)
        draft = result.data
        files = {item.path: item.content for item in draft.files}
        if len(files) != len(draft.files):
            raise ValidationGateError("技能候选重复定义了同一路径，未覆盖保存。")
        changes = []
        if original:
            changes = [{"path": path, "change": "removed" if path not in files else "added" if path not in original["texts"] else "modified"}
                for path in sorted(set(files) | set(original["texts"])) if files.get(path) != original["texts"].get(path)]
            files["assets/improvement-source.json"] = json.dumps({"schema_version": 1,
                "parent_id": original["id"], "parent_hash": original["hash"],
                "source_samples": [sample for item in evidence for sample in item["sample_ids"]],
                "author_request": message, "changes": changes, "status": "candidate"}, ensure_ascii=False, indent=2)
        saved = library.create(name=draft.name, description=draft.description, scope=scope, files=files)
        return {"status": "completed", "reply": draft.summary + "\n\n已保存为" + ("本书" if scope == "book" else "个人库")
            + "未启用候选。到设置 → 技能与提示词查看全部文件，核对后启用；也可以打开目录外部修改。",
            "skill": saved, "changes": changes, "skills_managed": True, "suggested_prompts": []}
    return None
