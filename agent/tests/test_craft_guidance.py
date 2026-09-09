from inkflow.craft import select_craft_guides


def test_craft_guidance_selects_hook_lifecycle_and_psychology_without_model_call() -> None:
    result = select_craft_guides(
        task="写出创伤后的回避，但本章还要推进伏笔并留下钩子",
        genre="都市悬疑",
        card={
            "foreshadow_advance": ["推进旧录音的来源"],
            "payoff": ["兑现门牌号异常"],
            "hook_question": "录音里为何出现主角的声音？",
        },
        limit=2,
    )

    keys = {item["技能编号"] for item in result}
    assert keys == {"hook-thread-lifecycle", "social-emotion"}
    assert all(item["本章如何使用"] for item in result)

