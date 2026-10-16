"""Small offline guard for the non-canon setting and source-version boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory

from inkflow.errors import ProjectError
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief
from inkflow.story_settings import StorySettingsService


def test_empty_settings_and_source_bound_review():
    import json
    from inkflow.schemas import ContextPacket, ContextSection, ContextUseAudit, ModeCheckOutput, SettingUseCheck
    from inkflow.review_verifier import packet_sources, setting_review_gaps
    with TemporaryDirectory() as temporary:
        project = InkFlowProject.create(Path(temporary) / "novel", BookBrief(
            title="设定覆盖检查", genre="悬疑", premise="主角在旧城寻找失踪亲人的故事与线索。", protagonist="林"))
        service = StorySettingsService(project)
        collection = service.save_collection(name="地点", template_id="location")
        payload = service.context_payload(actor="editor")
        assert payload["coverage"][0]["status"] == "empty"
        assert payload["collections"][0]["records"] == []
        for values in ({"layout": "  "}, {"layout": ["", None, {}]}):
            try:
                service.save_record(collection["collection_id"], title="空设定", values=values)
            except ProjectError as exc:
                assert "没有填写" in str(exc)
            else:
                raise AssertionError("Empty record must not be saved or reviewed")
        assert service.records(collection["collection_id"]) == []
        record = service.save_record(collection["collection_id"], title="锁铺", values={"layout": "锁铺只有一扇门。"})
        second = service.save_record(collection["collection_id"], title="码头", values={"layout": "码头有两扇门。"})
        payload = service.context_payload(actor="editor")
        packet = ContextPacket(project_id="test", chapter_no=1, task="核对设定", estimated_tokens=0,
            sections=[ContextSection(key="CSET", title="设定", content=json.dumps(payload, ensure_ascii=False),
                source_ids=[record["record_id"], second["record_id"]])])
        assert "码头" not in packet_sources(packet)[record["record_id"]]
        output = ModeCheckOutput(verdict="pass", confidence=1, summary="核对")
        assert len(setting_review_gaps(output, "林打开锁铺唯一的门。", packet)) == 2
        checks = [SettingUseCheck(record_id=record["record_id"], record_revision=record["revision"], disposition="compatible", setting_quote="锁铺只有一扇门。",
                    chapter_quote="林打开锁铺唯一的门。", reason="入口数量相同"),
                  SettingUseCheck(record_id=second["record_id"], record_revision=second["revision"], disposition="not_applicable", setting_quote="码头有两扇门。",
                    reason="本章没有出现或提及码头")]
        output.context_use_audit = ContextUseAudit(setting_checks=checks)
        assert not setting_review_gaps(output, "林打开锁铺唯一的门。", packet)
        checks[0].setting_quote = "码头有两扇门。"
        assert any("未定位" in gap for gap in setting_review_gaps(output, "林打开锁铺唯一的门。", packet))
        checks[0].setting_quote = "锁铺只有一扇门。"
        checks[0].disposition = "conflict"
        assert any("仍待核对" in gap for gap in setting_review_gaps(output, "林打开锁铺唯一的门。", packet))
        # A role without read permission receives neither records nor source labels.
        service.save_collection(name="地点", collection_id=collection["collection_id"], expected_revision=collection["revision"], read_roles=["writer"])
        assert all(item["collection_id"] != collection["collection_id"] for item in service.context_payload(actor="editor")["collections"])
        assert all(item["collection_id"] != collection["collection_id"] for item in service.context_payload(actor="editor")["coverage"])


def test_base_settings_and_optional_recording_switch():
    with TemporaryDirectory() as temporary:
        project = InkFlowProject.create(Path(temporary) / "novel", BookBrief(
            title="基础与高级设定", genre="悬疑", premise="主角在旧城寻找失踪亲人的故事与线索。", protagonist="林"))
        service = StorySettingsService(project)
        bases = [item for item in service.list_collections() if item.get("builtin")]
        assert {item["template_id"] for item in bases} == {"character", "world"}
        service.ensure_defaults()
        assert len(service.list_collections()) == 2
        world = next(item for item in bases if item["template_id"] == "world")
        constraint = service.save_record(world["collection_id"], title="当前人物边界",
            values={"premise": "主角在旧城寻找失踪亲人的故事与线索。"}, epistemic_status="user_constraint", actor="editor",
            evidence_refs=[{"source_kind": "planning", "path": "BOOK.md", "quote": "主角在旧城寻找失踪亲人的故事与线索。"}])
        assert constraint["epistemic_status"] == "user_constraint" and project.db.current_facts() == []
        try:
            service.save_record(world["collection_id"], title="当前人物边界",
                values=constraint["values"], epistemic_status="user_constraint", actor="editor",
                evidence_refs=constraint["evidence_refs"])
        except ProjectError as exc:
            assert "同名条目" in str(exc)
        else:
            raise AssertionError("A missing record ID must not duplicate an existing reviewed object")
        limited = service.context_payload(actor="editor", max_chars=1000)
        world_coverage = next(item for item in limited["coverage"] if item["collection_id"] == world["collection_id"])
        assert world_coverage["available_record_count"] == 1
        assert world_coverage["record_index"][0]["record_id"] == constraint["record_id"]
        assert world_coverage["status"] != "empty"
        for base in bases:
            try:
                service.archive_collection(base["collection_id"], base["revision"])
            except ProjectError:
                pass
            else:
                raise AssertionError("Base settings must remain available")
        advanced = service.save_collection(name="地点", template_id="location", instructions="只记录可确认的布局")
        record = service.save_record(advanced["collection_id"], title="锁铺", values={"layout": "一扇门"}, actor="writer")
        record = service.record_review(record["record_id"], record["revision"], decision="verified_reference")
        disabled = service.save_collection(name=advanced["name"], collection_id=advanced["collection_id"],
            expected_revision=advanced["revision"], auto_record=False)
        assert disabled["instructions"] == "只记录可确认的布局"
        payload = service.context_payload(actor="editor")
        selected = next(item for item in payload["collections"] if item["collection_id"] == advanced["collection_id"])
        assert selected["recording_allowed"] is False
        assert selected["records"][0]["status"] == "verified_reference"
        try:
            service.save_record(advanced["collection_id"], title="不应自动新增", values={"layout": "两扇门"}, actor="writer")
        except ProjectError as exc:
            assert "关闭自动记录" in str(exc)
        else:
            raise AssertionError("Opted-out settings must not receive model writes")
        assert service.records(advanced["collection_id"])[0]["values"] == record["values"]
        service.save_collection(name=advanced["name"], collection_id=advanced["collection_id"],
            expected_revision=disabled["revision"], auto_record=True)
        unchanged = service.save_record(advanced["collection_id"], record_id=record["record_id"],
            expected_revision=record["revision"], title=record["title"], values=record["values"], actor="writer")
        assert not unchanged["changed"] and unchanged["revision"] == record["revision"]


def test_setting_revisions_evidence_and_writer_authority():
    with TemporaryDirectory() as temporary:
        project = InkFlowProject.create(Path(temporary) / "novel", BookBrief(
            title="离线设定检查", genre="悬疑", premise="主角在旧城寻找失踪亲人的故事与线索。", protagonist="林"))
        service = StorySettingsService(project)
        assert len(service.templates()) >= 12
        collection = service.save_collection(name="主要人物", template_id="character")
        record = service.save_record(collection["collection_id"], title="林", values={"background": "旧城居民"},
                                     actor="writer", epistemic_status="objective")
        assert record["epistemic_status"] == "hypothesis" and record["authority"] == "reference_only"
        same = service.save_record(collection["collection_id"], record_id=record["record_id"], title="林",
                                   values=record["values"], actor="writer", expected_revision=record["revision"])
        assert same["changed"] is False and same["revision"] == record["revision"]
        try:
            service.save_record(collection["collection_id"], title="无依据补充", values={"background": "未知"},
                                actor="memory_keeper", operation="supplement")
        except ProjectError:
            pass
        else:
            raise AssertionError("Memory Keeper must not invent evidence-free references")
        source = project.root / "OUTLINE.md"
        source.write_text("林在旧城寻找亲人。", encoding="utf-8")
        supplemented = service.save_record(collection["collection_id"], record_id=record["record_id"], title="林",
            values={"traits": "寻找亲人"}, actor="memory_keeper", operation="supplement", expected_revision=record["revision"],
            evidence_refs=[{"source_kind": "planning", "path": "OUTLINE.md", "quote": "林在旧城寻找亲人。"}])
        assert supplemented["values"]["background"] == "旧城居民"
        reviewed = service.record_review(supplemented["record_id"], supplemented["revision"],
                                        decision="verified_reference", source_fingerprint=service.source_fingerprint())
        assert reviewed["authority"] == "reference_only" and project.db.current_facts() == []
        try:
            service.save_record(collection["collection_id"], record_id=record["record_id"], title="林",
                values={"background": "改写历史"}, actor="writer", expected_revision=reviewed["revision"])
        except ProjectError:
            pass
        else:
            raise AssertionError("Writer must make a separate hypothesis, not replace reviewed references")
        original_fingerprint = service.source_fingerprint()
        source.write_text("林已经离开旧城。", encoding="utf-8")
        assert original_fingerprint != service.source_fingerprint()
        assert "stale_evidence" in service.context()
        archived = service.archive_record(record["record_id"], reviewed["revision"])
        assert archived["status"] == "archived" and archived["history"]
        assert service.records(collection["collection_id"]) == []
        assert len(service.records(collection["collection_id"], include_archived=True)) == 1
