from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CraftGuide:
    key: str
    name: str
    triggers: tuple[str, ...]
    instruction: str
    basis: str


_GUIDES = (
    CraftGuide(
        key="causal-agency",
        name="人物目标与因果链",
        triggers=("目标", "决定", "代价", "冲突", "选择", "关系", "人物"),
        instruction=(
            "让人物带着具体目标进入场景；阻力迫使其选择，选择立刻改变关系、资源或风险。"
            "心理活动必须能追溯到人物所知、所怕或所求，不用性格标签替代行为。"
        ),
        basis="叙事理解依赖有目标的行动者、时间事件和可追踪因果。",
    ),
    CraftGuide(
        key="hook-thread-lifecycle",
        name="钩子与伏笔生命周期",
        triggers=("钩子", "伏笔", "回收", "兑现", "承诺", "foreshadow", "payoff", "hook"),
        instruction=(
            "本章至少推进或兑现一项既有承诺，再由本章新后果制造一个具体信息差。"
            "钩子要让读者知道自己在等什么，并让人物已经朝答案行动；不要另起与正文无关的突发事件。"
        ),
        basis="明确且可估计的信息缺口更容易引发求知；悬念规划需要追踪事件、读者预期与可能结果。",
    ),
    CraftGuide(
        key="pacing-concreteness",
        name="重要节点展开",
        triggers=("高潮", "转折", "追逐", "战斗", "揭示", "节奏", "升级", "余波"),
        instruction=(
            "先辨认本章最重要的状态变化：关键节点用动作、决定和即时后果写成场景；"
            "只负责搬运信息的连接段压缩，避免小事写满、大事一句带过。"
        ),
        basis="分层大纲需要区分事件的抽象层级与具体程度，才能稳定长篇节奏。",
    ),
    CraftGuide(
        key="suspense-information-gap",
        name="悬疑信息差",
        triggers=("悬疑", "秘密", "调查", "谜", "钩子", "线索", "凶", "真相"),
        instruction=(
            "只释放会改变人物判断或下一步行动的信息；线索出现要有观察来源和误判空间。"
            "章末留下一个读者能复述的具体问题，同时让人物已经付诸下一步行动。"
        ),
        basis="连续、可理解的因果更利于沉浸；信息差必须建立在人物视角与事件顺序上。",
    ),
    CraftGuide(
        key="social-emotion",
        name="关系中的情绪反应",
        triggers=("心理", "情绪", "创伤", "恐惧", "依恋", "亲密", "背叛", "羞耻", "愧疚"),
        instruction=(
            "把情绪写成注意到了什么、身体或语言怎样失衡、为了保护什么而采取什么行动。"
            "同一刺激因既往关系、信念和当下目标不同而产生不同反应；不要把虚构表现写成临床诊断。"
        ),
        basis="人物认同、情绪参与、可信感与叙事沉浸相关，但心理研究不能替代个体设定。",
    ),
    CraftGuide(
        key="dialogue-pressure",
        name="对白中的目的与压力",
        triggers=("对白", "对话", "审讯", "谈判", "争吵", "试探", "隐瞒"),
        instruction=(
            "每个说话者都在争取、躲避或试探某件事；台词表面信息和真实目的可以错位。"
            "用打断、回避、动作反应与称呼变化制造压力，不让人物轮流发表完整观点。"
        ),
        basis="人物投入依赖清晰的目标、视角、关系与情绪参与。",
    ),
)


def select_craft_guides(
    *,
    task: str,
    genre: str,
    card: dict[str, Any],
    limit: int = 2,
) -> list[dict[str, str]]:
    """Choose a tiny local craft packet; never calls a model or the network."""

    text = f"{task}\n{genre}\n{json.dumps(card, ensure_ascii=False)}".casefold()
    scored = [
        (sum(text.count(trigger.casefold()) for trigger in guide.triggers), index, guide)
        for index, guide in enumerate(_GUIDES)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = [guide for score, _, guide in scored if score > 0][: max(1, min(limit, 3))]
    psychology = next(guide for guide in _GUIDES if guide.key == "social-emotion")
    explicit_psychology = any(trigger in task.casefold() for trigger in psychology.triggers)
    if explicit_psychology and psychology not in selected:
        selected = [*selected[: max(0, min(limit, 3) - 1)], psychology]
    if not selected:
        selected = [_GUIDES[0], _GUIDES[1]][: max(1, min(limit, 2))]
    return [
        {
            "技能": guide.name,
            "本章如何使用": guide.instruction,
            "依据摘要": guide.basis,
            "技能编号": guide.key,
        }
        for guide in selected
    ]
