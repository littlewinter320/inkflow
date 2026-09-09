from inkflow.schemas import ClarificationOption, ClarificationQuestion, TerminalIntent
from inkflow.terminal_session import TerminalSession


def test_question_cards_keep_guidance_and_append_other_last() -> None:
    intent = TerminalIntent(
        action="discuss",
        requested_outcome="确定开篇重心",
        confidence="medium",
        visible_reason="两个方向都会明显改变开篇。",
        clarification_questions=[
            ClarificationQuestion(
                header="开篇重心",
                question="第一章更想先抓住哪种体验？",
                why_it_matters="这会改变第一章的场景目标与章末钩子。",
                options=[
                    ClarificationOption(label="先破案", description="更快进入外部冲突。", recommended=True),
                    ClarificationOption(label="先写关系", description="先建立人物情感债。"),
                ],
            )
        ],
    )

    cards = TerminalSession._question_cards(intent)

    assert cards[0]["question"] == "第一章更想先抓住哪种体验？"
    assert cards[0]["why_it_matters"].startswith("这会改变")
    assert [item["label"] for item in cards[0]["options"]] == ["先破案", "先写关系", "其他"]
    assert cards[0]["options"][-1]["kind"] == "other"

