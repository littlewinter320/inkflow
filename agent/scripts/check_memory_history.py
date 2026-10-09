"""One offline check for historical gaps and bounded local retrieval; run manually."""
from pathlib import Path
from tempfile import TemporaryDirectory

from inkflow.database import ProjectDatabase
from inkflow.retrieval import HybridRetriever
from inkflow.schemas import MemoryPatch, ThreadMutation
from inkflow.utils import content_hash


def check() -> None:
    with TemporaryDirectory() as directory:
        db = ProjectDatabase(Path(directory) / "history.sqlite3")
        texts = {1: "铁盒被藏在板房。", 3: "真相在第三章揭晓。"}
        with db.connect() as connection:
            for number, text in texts.items():
                connection.execute("INSERT INTO chapters(chapter_no,title,status,version,path,content_hash,content_text,updated_at) VALUES (?,?,'accepted',1,?,?,?,'now')",
                    (number, str(number), f"chapter_{number}.md", content_hash(text), text))
            connection.execute("INSERT INTO plot_threads(thread_id,kind,title,status,description,planted_chapter,last_advanced_chapter,source_version,source_hash,updated_at) VALUES ('mystery','mystery','第三章真相','paid',?,1,3,1,?,'now')",
                (texts[3], content_hash(texts[3])))
            connection.commit()
        early = db.threads_state_as_of(1)
        assert not early["threads"] and early["coverage_gaps"][0]["lifecycle_state"] == "unknown"
        assert early["recovery_sources"][0]["content"] == texts[1]
        assert "第三章真相" not in str(early) and texts[3] not in str(early)
        patch = MemoryPatch(chapter_no=1, chapter_summary="铁盒被藏好。", threads=[
            ThreadMutation(thread_id="mystery", kind="mystery", title="铁盒", status="open",
                           description=texts[1], planted_chapter=1)])
        with db.connect() as connection:
            connection.execute("INSERT INTO memory_patches(chapter_no,data_json,committed_at) VALUES (1,?,'now')", (patch.model_dump_json(),))
            connection.commit()
        assert db.threads_as_of(1)[0]["status"] == "open"
        assert not db.threads_as_of(3)  # Missing later event must not revive old open state.
        assert db.threads_state_as_of(3)["coverage_gaps"][0]["evidence_quote"] == texts[3]

    def candidate(key: str, entity: str, category: str = "facts") -> dict:
        return {"source_id": key, "source_type": "canon_fact", "title": key, "body": entity,
                "entities": [entity], "authority_rank": 2, "category": category, "identity": "accepted_fact"}
    rows = [candidate("first", "甲"), candidate("second", "乙"), candidate("related", "甲")]
    assert HybridRetriever._relation_expand(["first", "second"], rows, limit=2) == ["first", "related"]
    merged, overlap, conflict = HybridRetriever._merge_candidates([rows[0], dict(rows[0])])
    assert len(merged) == 1 and overlap and not conflict
    wrong = {**rows[0], "body": "来源不一致"}
    assert HybridRetriever._merge_candidates([rows[0], wrong])[2][0]["excluded"]
    retriever = object.__new__(HybridRetriever)
    _, _, branches = retriever._local_branches("甲", merged)
    assert any(item["category"] == "facts" and item["state"] == "matched" for item in branches)
    assert any(item["category"] == "threads" and item["state"] == "empty" for item in branches)


if __name__ == "__main__":
    check()
    print("Historical memory and bounded retrieval check passed.")
