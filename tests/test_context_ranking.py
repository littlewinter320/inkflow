from inkflow.context import _rank_chapter_summaries


def test_related_old_chapter_summary_is_recalled_without_loading_every_chapter() -> None:
    chapters = [
        {"chapter_no": 1, "title": "青铜钥匙", "summary": "林照在旧仓库拿到青铜钥匙，钥匙背面刻着潮汐日期。"},
        {"chapter_no": 2, "title": "雨夜追车", "summary": "林照追踪黑色轿车，在高架桥下失去目标。"},
        {"chapter_no": 8, "title": "名单", "summary": "调查组确认烧焦名单里缺少一个人的名字。"},
        {"chapter_no": 9, "title": "回声", "summary": "上一章的争执改变了调查组分工。"},
    ]

    result = _rank_chapter_summaries(
        chapters,
        "本章要用青铜钥匙背后的潮汐日期打开仓库暗门",
        before_chapter=10,
        excluded_chapters={9},
        limit=3,
    )

    assert result[0]["章节"] == 1
    assert all(item["章节"] != 9 for item in result)
    assert len(result) <= 3
