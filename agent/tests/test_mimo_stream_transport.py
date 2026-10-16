import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.provider import DeepSeekProvider


class StreamProbe(BaseModel):
    answer: str


class SplitBytes(httpx.AsyncByteStream):
    def __init__(self, text):
        self.data=text.encode("utf-8")

    async def __aiter__(self):
        for start in range(0,len(self.data),3):
            yield self.data[start:start+3]


def test_mimo_sse_chunks_incomplete_retry_http_errors_and_optional_usage(tmp_path,monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH",str(tmp_path/"settings.json"))
    monkeypatch.setattr(Settings,"require_api_key",lambda self:"offline-test")
    requests=[]
    mode="complete"
    incomplete_sent=False
    usage={"prompt_tokens":90,"completion_tokens":12,"total_tokens":102,
           "prompt_tokens_details":{"cached_tokens":64},"completion_tokens_details":{"reasoning_tokens":2}}
    def event(data):
        return "data: "+json.dumps(data,ensure_ascii=False)+"\n\n"
    def respond(request):
        nonlocal incomplete_sent
        payload=json.loads(request.content)
        requests.append(payload)
        if mode=="http_error":
            return httpx.Response(402,json={"error":{"message":"Insufficient account balance"}})
        if mode=="deepseek":
            return httpx.Response(200,json={"choices":[{"message":{"content":'{"answer":"原格式"}'},"finish_reason":"stop"}],"usage":usage})
        chunks=[{"id":"stream-response","model":"mimo-v2.6-flash","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"内部片段"},"finish_reason":None}],"usage":None}]
        chunks += [{"choices":[{"index":0,"delta":{"content":part},"finish_reason":None}]} for part in ['{"answer":','"墨','流"}']]
        text=": heartbeat\n\n"+"".join(event(chunk) for chunk in chunks)
        if mode=="incomplete_then_eof" and not incomplete_sent:
            incomplete_sent=True
        else:
            text += event({"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
            if mode=="complete":
                text += event({"choices":[],"usage":usage})+"data: [DONE]\n\n"
        return httpx.Response(200,headers={"content-type":"text/event-stream; charset=utf-8"},stream=SplitBytes(text))
    client=httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient",lambda **kwargs:client(transport=httpx.MockTransport(respond),**kwargs))
    async def no_delay(_):
        return None
    monkeypatch.setattr("inkflow.provider.asyncio.sleep",no_delay)
    settings=Settings(provider_kind="mimo",base_url="https://token-plan-cn.xiaomimimo.com/v1",model="mimo-v2.6-flash",
        quality_preset="economy",workspace_root=tmp_path,agent_generation={"writer":{"temperature":.7,"top_p":.9}})
    provider=DeepSeekProvider(settings)
    args=dict(system_prompt="保持结构化输出",user_prompt="检查流式传输",output_model=StreamProbe,agent_role="writer",thinking=False)
    complete=asyncio.run(provider.generate_json(**args))
    assert complete.data.answer=="墨流" and complete.response_id=="stream-response"
    assert complete.reasoning_content=="内部片段" and complete.model=="mimo-v2.6-flash"
    assert complete.usage["prompt_tokens"]==90 and complete.usage["prompt_tokens_details"]==usage["prompt_tokens_details"]
    assert complete.usage["completion_tokens_details"]==usage["completion_tokens_details"]
    assert len(requests)==1 and requests[0]["stream"] is True and "stream_options" not in requests[0]
    assert requests[0]["temperature"]==.7 and requests[0]["top_p"]==.9 and requests[0]["response_format"]=={"type":"json_object"}

    mode="incomplete_then_eof"
    before=len(requests)
    recovered=asyncio.run(provider.generate_json(**args))
    assert recovered.data.answer=="墨流" and len(requests)==before+2
    assert requests[-1]["messages"]==requests[-2]["messages"] and requests[-1]["thinking"]==requests[-2]["thinking"]
    assert recovered.usage["recovery_events"][0]["kind"]=="transport"
    assert [item["attempt"] for item in recovered.usage["attempt_usage"]]==[1,2]
    assert recovered.usage.get("prompt_tokens") is None and recovered.usage["attempt_usage"][-1]["usage"]=={}

    mode="http_error"
    before=len(requests)
    with pytest.raises(ProviderError,match="HTTP 402.*Insufficient account balance"):
        asyncio.run(provider.generate_json(**args))
    assert len(requests)==before+1

    mode="deepseek"
    deepseek=DeepSeekProvider(Settings(workspace_root=tmp_path))
    prefix="稳定参考资料\n"
    result=asyncio.run(deepseek.generate_json(**{**args,"user_prompt":prefix+"当前任务","cache_prefix":prefix}))
    assert result.data.answer=="原格式" and requests[-1]["stream"] is False
    assert len(requests[-1]["messages"])==3 and requests[-1]["messages"][1]["content"]==prefix


def test_mimo_sse_terminal_refusals_and_explicit_permanent_errors_do_not_retry(tmp_path,monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH",str(tmp_path/"settings.json"))
    monkeypatch.setattr(Settings,"require_api_key",lambda self:"offline-test")
    mode={"finish_reason":"tool_calls"}
    requests=[]
    def respond(request):
        requests.append(json.loads(request.content))
        chunk=mode if "error" in mode else {"choices":[{"delta":{"content":'{"answer":"must reject"}'},"finish_reason":mode["finish_reason"]}]}
        return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=SplitBytes("data: "+json.dumps(chunk)+"\n\ndata: [DONE]\n\n"))
    client=httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient",lambda **kwargs:client(transport=httpx.MockTransport(respond),**kwargs))
    async def no_delay(_):
        return None
    monkeypatch.setattr("inkflow.provider.asyncio.sleep",no_delay)
    provider=DeepSeekProvider(Settings(provider_kind="mimo",base_url="https://token-plan-cn.xiaomimimo.com/v1",model="mimo-v2.6-flash",
        quality_preset="economy",workspace_root=tmp_path))
    args=dict(system_prompt="离线终止边界",user_prompt="不能交付未正常完成的内容",output_model=StreamProbe,thinking=False)
    for reason in ("tool_calls","content_filter","unknown_terminal"):
        mode={"finish_reason":reason}
        before=len(requests)
        with pytest.raises(ProviderError,match="未正常完成"):
            asyncio.run(provider.generate_json(**args))
        assert len(requests)==before+1
    mode={"finish_reason":"length"}
    before=len(requests)
    with pytest.raises(ProviderError,match="响应被截断"):
        asyncio.run(provider.generate_json(**args))
    assert len(requests)==before+1
    for error in ({"code":"invalid_api_key","message":"Invalid API Key"},
                  {"message":"Insufficient account balance"},
                  {"type":"invalid_request_error","message":"parameter rejected"}):
        mode={"error":error}
        before=len(requests)
        with pytest.raises(ProviderError,match="流式响应返回请求错误"):
            asyncio.run(provider.generate_json(**args))
        assert len(requests)==before+1
    mode={"error":{"code":"not_documented_here","message":"Try again later"}}
    before=len(requests)
    with pytest.raises(ProviderError,match="模型请求尝试 2 次"):
        asyncio.run(provider.generate_json(**args))
    assert len(requests)==before+2
