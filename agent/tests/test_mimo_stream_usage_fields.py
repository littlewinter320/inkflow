import asyncio
import json

import httpx

from inkflow.provider import _mimo_stream_response


def test_mimo_sse_usage_merges_reported_fields_without_inventing_counts():
    scenarios = [
        ([{"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80},
           "completion_tokens_details": {"reasoning_tokens": 5}},
          {"completion_tokens": 12, "completion_tokens_details": {"accepted_prediction_tokens": 2}},
          {"total_tokens": 112}],
         {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80},
          "completion_tokens": 12, "completion_tokens_details": {
              "reasoning_tokens": 5, "accepted_prediction_tokens": 2}, "total_tokens": 112}),
        ([{"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}},
          {"prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens": 2}],
         {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens": 2}),
        ([{"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}},
          {"prompt_tokens": None, "prompt_tokens_details": {"cached_tokens": None}},
          {"completion_tokens_details": {"reasoning_tokens": 5}},
          {"completion_tokens_details": None}],
         {"prompt_tokens": None, "prompt_tokens_details": {"cached_tokens": None},
          "completion_tokens_details": None}),
        ([None, {}, {"completion_tokens": 2}, None], {"completion_tokens": 2}),
    ]
    requests = []

    async def check():
        for chunks, expected in scenarios:
            events = [{"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}]
            events.extend({"choices": [], "usage": usage} for usage in chunks)
            data = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"

            def respond(request):
                requests.append(request)
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                response = await _mimo_stream_response(client, "https://offline.invalid/chat/completions",
                                                       headers={}, payload={"stream": True})
            assert response.json()["usage"] == expected
        assert len(requests) == len(scenarios)

    asyncio.run(check())
