from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .humanizer import HUMANIZER_ZH_CONTRACT


# Shared, static expression guidance. Source observations live in the author-style
# document; manuscript text, story facts and per-run statistics stay out of prompts.
AUTHOR_STYLE_DEFAULT = {
    "name": "第一阶段样稿表达",
    "basis": "从用户提供的第一阶段章节提炼，重点采用第9—13章的口语、人物盘算、句段衔接和情绪反应；下方示例为匿名表达演示。",
    "priority": "默认启用这套基础写作技能，不依赖外部文件或每次提醒。当前明确要求、本书偏好和已确认作者习惯仍可调整；保留本书题材、视角及人物身份，不把历史生成的旧文风当作拒绝新要求的依据。",
    "guides": [
        {"name": "句子接得上", "guidance": "沿人物的观察、判断、动作和后果往下写，同一口气尚未说完，用中文逗号或必要的连接词接住；意思收束、行动转折或重音落定才用句号。重要的突然反应可用短句，其余不把每个动作切成一行。长句保持主语、指代和前后关系清楚，不追求句长或标点比例。"},
        {"name": "用人物会说的词", "guidance": "优先具体、顺口的动词与贴合身份的说法，让对白有回应、顾虑、打断或自我修正，不让所有人替旁白解释结论。口语、吐槽、生活化比喻和热梗按人物年龄、题材与当下关系选择，不固定配额，不反复用同一梗；正式叙述和年长人物也能自然，不硬塞网络口头禅。避免空泛升华、同义排比和反复解释已经读得懂的结论。"},
        {"name": "情绪随事情变化", "guidance": "从当前刺激接到人物在意什么，再接到身体反应、内心短念或小动作；可以直接表达，也可以留给读者体会，不每次凑齐同一组三层。紧张中的旁想应来自人物的处境，并能回到眼前行动。感官和拟声有现场来源，不用抽象情绪标签或全知旁白替人物证明猜测。"},
        {"name": "标点跟着语气走", "guidance": "中文叙述使用，和。；对白用成对的“”，引号内再引用用‘’，普通名词或强调词不滥加引号。？用于真正的疑问、自问或反问，！用于突然反应、喊话或确有强度的情绪，普通叙述不用连续感叹号抬高音量。句末符号放在对白引号内；对白后还有动作时按句意衔接，不能漏掉结尾。冒号只在需要引出话语时使用，不给每句话挂冒号。"},
        {"name": "内心戏与分段", "guidance": "简短、带人物声音的内心插话可用中文（），较长的心理活动另起段并留空行；不把检查清单、作者补充或剧情解释塞进括号。换说话人、场景或注意力时换段，同一人物连续动作与对应对白可在同段。破折号用于打断或突转，省略号用于迟疑和话未说完；拟声可独立强调，但不靠碎段、加粗或一串符号替代节奏。"},
        {"name": "行动逻辑读得懂", "guidance": "关键选择写清人物当时知道什么、为什么这样做、行动怎样接上前因及付出什么代价；重要物件的获取、转移和使用可追踪。过渡与日常可压缩，不为审核重复清点纸张、编号和收纳位置。判断、传闻与已证实事实分开；读者只读正文也应理解必要的因果，不依赖章外说明补洞。"},
        {"name": "保留表达，修正病句", "guidance": "交稿前按阅读顺序检查搭配不当、缺主语、指代串人、重复成分、因果倒置及引号括号不配对，定点修正，保留自然的长短句和人物语气。借用样稿的表达方法，不复制独特句子、人物、情节或世界规则，不模仿错字、漏标点与数值矛盾。系统面板仅用于本书确有该设定的场景。个别作品的禁词、符号和面板要求只在当前明确授权的范围使用，不强加给所有小说。"},
    ],
    "examples": [
        {
            "name": "让人物的算盘带着动作走",
            "before": "他感到经济压力很大，决定再考虑一下。",
            "after": "房租、水电，还有那笔没还清的钱，他在心里算了一遍，越算越想把账单塞回去。（不看就不用付了？想得倒美。）他抹了把脸，又把账单摊开。",
            "method": "先落到人物真正担心的东西，再让短念头回到行动；只使用本书已有处境，不把示例账目编进正文。",
        },
        {
            "name": "对白有回应和人物口气",
            "before": "“你去不去？”“去。”他回答，然后起身。",
            "after": "“你去不去？”\n\n“你都站门口了，我还能说不去？”他嘴上不肯吃亏，手已经伸向椅背上的外套。",
            "method": "同样的答应可以带顾虑、打趣或反驳。语气须符合当前关系，严肃人物也有自己的口吻；不把所有人写成同一种吐槽者。",
        },
        {
            "name": "情绪接到眼前选择",
            "before": "他很紧张，拿起杯子，喝了一口水，放下杯子。",
            "after": "他端起杯子想缓一缓，杯沿却磕了下牙。声音不大，他自己听得清清楚楚，索性把杯子放回去，看向对面：“你先说，究竟怎么回事？”",
            "method": "一个反应足以带出情绪和下一步，不反复清点手、纸、灯和位置；新增对白与动作不得改动事实或人物所知。",
        },
        {
            "name": "在句意转向时补标点",
            "before": "他沿墙往前走脚下尽量避开碎石这会儿弄出动静不是给自己找事吗",
            "after": "他沿墙往前走，脚下尽量避开碎石。这会儿弄出动静，不是给自己找事吗？",
            "method": "连续动作接逗号，念头转向收句，真实反问用问号。只修当前病句；对白引号、短念头括号和感叹号按语意使用，不规定次数。",
        },
    ],
}

AUTHOR_VOICE_CONTRACT = (
    "内置基础写作技能：" + AUTHOR_STYLE_DEFAULT["name"] + "\n" + AUTHOR_STYLE_DEFAULT["priority"] + "\n"
    + "\n".join(f"- {guide['name']}：{guide['guidance']}" for guide in AUTHOR_STYLE_DEFAULT["guides"])
    + "\n表达示例只展示方法，不是本书事实，不复制人物、物件、情节或原句；按当前视角和场景迁移：\n"
    + "\n".join(
        f"{example['name']}\n问题表达：{example['before']}\n表达示范：{example['after']}\n使用方法：{example['method']}"
        for example in AUTHOR_STYLE_DEFAULT["examples"]
    )
    + "\n审核表达时同样按这些要求阅读当前段落，指出确实影响人物声音或阅读的原句与原因；普通风格差异不冒充事实矛盾，不用标点次数打分，不因单个口语词推定作者身份。\n"
)


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
    """Always bind the built-in voice skill, then select a relevant optional hint.

    Genre alone is too broad to decide how a scene should be written. An
    unrelated or negative instruction must not enable a scene-specific template.
    The shared system contract supplies the full voice skill once; this manifest
    records its use without duplicating the rules or calling a model/network.
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
    return [{
        "技能": AUTHOR_STYLE_DEFAULT["name"],
        "本章如何使用": "默认使用系统词中的基础技能和表达示例，按本书视角迁移人物盘算、对白回应、情绪与动作、标点及段落衔接。写作、局部修订和表达审查使用同一依据；不复制示例事实，不以符号数量判定通过。",
        "技能编号": "author-sample-voice",
        "技能版本": "1",
    }] + [
        {
            "技能": guide.name,
            "本章如何使用": guide.instruction,
            "技能编号": guide.key,
            "技能版本": "1",
        }
        for guide in selected
    ]
