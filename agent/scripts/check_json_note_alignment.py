"""Check complete JSON objects and references after the existing prose format step."""
import json
import ast
from pathlib import Path
from types import SimpleNamespace
from pydantic import ValidationError
from inkflow.provider import _model_from_json_content
from inkflow.schemas import DraftOutput, WriterNoteClarification
from inkflow.writer_notes import align_annotation_quotes, annotation_gaps, version_notes
from inkflow.errors import ProviderError
from inkflow.utils import content_hash


def check():
    body = "上截是看得见、对得上的四样，下截空着。" + "灯下的纸面没有其他改动。" * 12
    note = {"annotations": [{"kind":"new_fact","statement":"四样纸面事实", "quote":"上截是看得见、对得上的四样，下截空着。"}]}
    result = _model_from_json_content(json.dumps({"title":"两份单子","content":body,"hook_note":note},ensure_ascii=False), DraftOutput)
    assert not annotation_gaps(result.hook_note.model_dump(), result.content)
    assert note["annotations"][0]["quote"] == "上截是看得见、对得上的四样，下截空着。"
    aligned, changes = align_annotation_quotes(note, result.content)
    assert changes[0]["original_quote"] == note["annotations"][0]["quote"]
    assert aligned["annotations"][0]["quote"] in result.content
    chapter={"version":5,"content_text":result.content,"content_hash":content_hash(result.content)}
    artifact={"artifact_id":"old-note","chapter_version":5,"status":"current","data":note}
    project=SimpleNamespace(db=SimpleNamespace(get_chapter=lambda number:chapter,
        list_agent_artifacts=lambda **kwargs:[artifact] if kwargs["artifact_type"]=="writer_hook_note" else []))
    projected=version_notes(project,31)
    assert projected["quote_alignments"][0]["original_quote"] == note["annotations"][0]["quote"]
    assert not annotation_gaps(projected["hook_note"],result.content)
    wrong = {"annotations":[{"kind":"new_fact","statement":"错误事实","quote":"上截是三样、另有一张收据。"}]}
    assert align_annotation_quotes(wrong,result.content) == (wrong,[])
    complete = '{"answers":["原句中有 { 括号 } 和转义\\\"字符"],"corrected_hook_note":null}'
    assert _model_from_json_content("说明如下：\n" + complete + "\n结束", WriterNoteClarification).answers
    truncated = '{"answers":["未结束"],"corrected_hook_note":{"annotations":[]}'
    try:
        _model_from_json_content(truncated,WriterNoteClarification)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("Truncated outer JSON must not adopt a nested object")
    nested = '{"wrapper":{"answers":["不能冒充根结果"]}}'
    try:
        _model_from_json_content("说明：" + nested,WriterNoteClarification)
    except ValidationError:
        pass
    else:
        raise AssertionError("Nested objects must not replace the response root")
    source=Path(__file__).parents[1]/"src/inkflow/provider.py"
    tree=ast.parse(source.read_text("utf-8"))
    branch=next(node for node in ast.walk(tree) if isinstance(node,ast.If)
        and ast.unparse(node.test)=="choice.get('finish_reason') == 'length'")
    wrapper=ast.parse("def cap(choice,requested_max_tokens):\n pass").body[0]
    wrapper.body=[branch]
    namespace={"ProviderError":ProviderError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(source),"exec"),namespace)
    namespace["cap"]({"finish_reason":"stop"},8192)
    try:
        namespace["cap"]({"finish_reason":"length"},8192)
    except ProviderError as error:
        assert "8192" in str(error) and "截断" in str(error)
    else:
        raise AssertionError("Length response must not enter JSON approval")
    print("JSON and note alignment checks passed: complete root, escaped quotes/braces, truncated root rejected, punctuation alignment and false fact retained.")


if __name__ == "__main__":
    check()
