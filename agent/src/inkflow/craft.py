from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .humanizer import HUMANIZER_ZH_CONTRACT


# Shared, static expression guidance. Source observations live in the author-style
# document; manuscript text, story facts and per-run statistics stay out of prompts.
AUTHOR_VOICE_CONTRACT = """作者样稿文风（默认表达偏好，当前用户要求与本书已建立的声音优先）：
- 用人物自己的语气叙述，观察、判断和动作可以沿同一条意脉自然延伸；主意尚未说完就用必要的逗号或连接词承接，意思收束、行动转折或重音落定时再用句号。短句可以点出突然的发现或反应，不把每个动作、感受都切成单句单段；长句也须有清楚的主语和前后关系，不追求固定句长或标点比例。
- 用具体、顺口的动词和人物会说的话，允许自我修正、反问、生活化比喻和有处境的吐槽。口语词、热梗和夸张跟着人物身份、题材和当下情绪走，不规定每章数量，不机械复用同一梗，不把正式叙事全部改成网络段子。人物自己可有偏见，旁白不替其证明猜测。
- 中文逗号连接同一意脉，不能因数量多就拆短句；直接对白用成对中文双引号，普通名词和强调词不滥加引号。冒号用于真正引出话语或提示，不给每个动作都挂冒号。中文括号可容纳人物短暂的内心插话，较长内心戏可另起一段；不能把作者解释、检查清单塞进括号。破折号用于突转或打断，省略号用于迟疑或未尽话意，拟声要有现场来源；不拿这些符号代替正常衔接。
- 场景、说话人或注意力转移时换段，动作和对应对白可在同段；短暂旁想后能接回当前行动。只在作品确有系统面板时使用独立的 [提示]，不把原作的人名、技能、数值和世界规则带进别的故事。保留必要标点、正确指代和可读语法，不模仿样稿中的错字、漏引号或数值矛盾。此前明确禁用的中文顿号“、”继续禁用，中文语境使用中文逗号。
"""


AUTHOR_VOICE_CONTRACT += "\n" + HUMANIZER_ZH_CONTRACT


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
            "围绕人物处境和目的组织场景，让行动与选择接上前因；关系、资源或风险的后果可以当场发生，也可以延后显现，不为每场硬凑冲突。"
            "心理活动必须能追溯到人物所知、所怕或所求，不用性格标签替代行为。"
        ),
        basis="叙事理解依赖有目标的行动者、时间事件和可追踪因果。",
    ),
    CraftGuide(
        key="hook-thread-lifecycle",
        name="钩子与伏笔生命周期",
        triggers=("钩子", "伏笔", "回收", "兑现", "承诺", "foreshadow", "payoff", "hook"),
        instruction=(
            "只有当章节卡或前文确有待处理的承诺时，才考虑推进、暂缓或兑现，并让暂缓有剧情理由。"
            "新钩子不是每章必需；如果本章后果自然留下具体问题，再让读者看懂人物接下来会怎么查。"
            "不为制造悬念添加与人物选择和既有剧情无关的突发事件。"
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
            "情绪可以直接说出，也可以从人物注意到的细节、身体反应、口吻或行动显露；按当下处境选择，不强制每次凑齐三层。"
            "同一刺激因既往关系、信念和当下目标不同而产生不同反应；不要把虚构表现写成临床诊断。"
        ),
        basis="人物认同、情绪参与、可信感与叙事沉浸相关，但心理研究不能替代个体设定。",
    ),
    CraftGuide(
        key="dialogue-pressure",
        name="对白中的目的与压力",
        triggers=("对白", "对话", "审讯", "谈判", "争吵", "试探", "隐瞒"),
        instruction=(
            "对白符合说话者所知、关系和当下目的，台词表面信息和真实想法可以错位。"
            "谈判可有打断和回避，闲聊也可自然展开；称呼、语气和沉默体现人物差异，不要求每句对白后立即改变局面。"
        ),
        basis="人物投入依赖清晰的目标、视角、关系与情绪参与。",
    ),
)


def select_craft_guides(
    *,
    task: str,
    genre: str,
    card: dict[str, Any],
    limit: int = 1,
) -> list[dict[str, str]]:
    """Select only a locally relevant optional hint; never calls a model/network.

    Genre alone is too broad to decide how a scene should be written. An
    unrelated or negative instruction must not cause a fixed craft template
    to be inserted into every chapter's prompt.
    """
    chapter_fields = (
        "title_working", "function", "goal", "obstacle", "decision",
        "consequence", "irreversible_delta", "scenes", "information_release",
        "foreshadow_advance", "payoff", "withholding_boundary",
    )
    card_text = "\n".join(str(card.get(key) or "") for key in chapter_fields).casefold()
    task_text = str(task).casefold()
    selection_text = f"{task_text}\n{card_text}"
    # Strip explicit exclusions before lexical selection so "不要新伏笔" does
    # not itself activate the hook guide. Other positive requirements survive.
    selection_text = re.sub(
        r"(?:不要|别|无需|不需要|不再)[^，。；;！？!?\n]*",
        " ",
        selection_text,
    )
    scored = []
    for index, guide in enumerate(_GUIDES):
        score = sum(selection_text.count(trigger.casefold()) for trigger in guide.triggers)
        if score:
            scored.append((score, index, guide))
    scored.sort(key=lambda item: (-item[0], item[1]))
    slot_count = max(1, min(limit, 2))
    selected = [guide for _, _, guide in scored[:slot_count]]
    return [
        {
            "技能": guide.name,
            "本章如何使用": guide.instruction,
            "技能编号": guide.key,
            "技能版本": "1",
        }
        for guide in selected
    ]
