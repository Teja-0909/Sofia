"""Exercise real provider adapters through offline HTTP transports, never live APIs."""
import asyncio
import json
import logging
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm
from app import research_engine as engine


def mock_http(monkeypatch, handler):
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=transport, trust_env=False, **kwargs))
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "offline-test-key")


def gemini_response(value):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 40,
                          "thoughtsTokenCount": 10},
    })


@pytest.mark.parametrize("response_format", [
    engine._PLAN_SCHEMA, engine._SOLVE_SCHEMA, engine._VERIFY_SCHEMA,
])
def test_research_schemas_use_gemini_json_schema_wire_field(monkeypatch, response_format):
    original = deepcopy(response_format)
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        generation = body["generationConfig"]
        assert generation["responseJsonSchema"] == original["json_schema"]["schema"]
        assert "responseSchema" not in generation
        assert generation["responseMimeType"] == "application/json"
        assert generation["maxOutputTokens"] == 1234
        assert "tools" not in body
        return gemini_response({"ok": True})

    mock_http(monkeypatch, handler)
    text, usage, tools = asyncio.run(llm._call_gemini(
        "policy", [{"role": "user", "content": "public task"}], "configured-primary",
        response_format, max_output_tokens=1234))
    assert json.loads(text) == {"ok": True}
    assert usage == {"prompt_tokens": 100, "completion_tokens": 50}
    assert tools == []
    assert response_format == original
    assert len(requests) == 1
    assert requests[0].url.path == "/v1beta/models/configured-primary:generateContent"


def test_full_research_pipeline_reaches_three_real_gemini_wire_calls(monkeypatch):
    """Do not mock _call_gemini: planner, solver and verifier must all hit HTTP."""
    schemas = [engine._PLAN_SCHEMA, engine._SOLVE_SCHEMA, engine._VERIFY_SCHEMA]
    originals = deepcopy(schemas)
    results = [
        {"queries": ["current official release limitations"]},
        {"conclusions": [{"text": "The current release has documented limitations.",
                          "citations": ["E1"]}], "uncertainties": [],
         "sufficient": True, "follow_up_queries": []},
        {"supported": [0], "uncertainties": []},
    ]
    requests = []

    def handler(request):
        index = len(requests)
        requests.append(request)
        body = json.loads(request.content)
        generation = body["generationConfig"]
        assert generation["responseJsonSchema"] == schemas[index]["json_schema"]["schema"]
        assert "responseSchema" not in generation
        assert generation["responseMimeType"] == "application/json"
        assert generation["maxOutputTokens"] == 2048
        assert "tools" not in body
        assert request.url.path == "/v1beta/models/configured-primary:generateContent"
        return gemini_response(results[index])

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(engine, "_MODEL_SLOTS", asyncio.Semaphore(2))
    monkeypatch.setattr(llm, "_provider_chain", lambda: [
        ("gemini", "configured-primary", "gemini"),
        ("gemini", "must-not-fall-back", "gemini"),
    ])
    monkeypatch.setattr(engine.config, "ENABLE_RESEARCH_BROWSER", False)
    monkeypatch.setattr(engine.search, "search_web", AsyncMock(return_value=[{
        "url": "https://docs.example.com/release", "title": "Current official release",
        "snippet": "Current release limitations are documented here.",
    }]))
    monkeypatch.setattr(engine.search, "fetch_page_receipt", AsyncMock(return_value={
        "url": "https://docs.example.com/release", "title": "Current official release",
        "text": "The current official release documentation describes its limitations. " * 8,
        "truncated": False, "status": "retrieved",
    }))

    result = asyncio.run(engine.run_research(
        "What are the current official release limitations?",
        checkpoint=AsyncMock(), progress=AsyncMock()))
    assert result["status"] == "completed"
    assert "[E1]" in result["answer"]
    assert len(requests) == result["usage"]["model_calls"] == 3
    assert result["usage"]["input_tokens"] == 300
    assert result["usage"]["output_tokens"] == 150
    assert result["usage"]["output_token_reservations"] == 6144
    assert schemas == originals


def test_research_http_rejection_is_diagnosable_without_leaks_or_fallback(monkeypatch, caplog):
    query_sentinel = "private-query-sentinel"
    body_sentinel = "private-upstream-body-sentinel"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1beta/models/configured-primary:generateContent"
        assert "responseJsonSchema" in json.loads(request.content)["generationConfig"]
        return httpx.Response(400, json={"error": {"message": body_sentinel}})

    mock_http(monkeypatch, handler)
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(engine, "_MODEL_SLOTS", asyncio.Semaphore(2))
    monkeypatch.setattr(llm, "_provider_chain", lambda: [
        ("gemini", "configured-primary", "gemini"),
        ("groq", "must-not-fall-back", "openai"),
    ])
    monkeypatch.setattr(engine.config, "ENABLE_RESEARCH_BROWSER", False)
    monkeypatch.setattr(engine.search, "search_web", AsyncMock(return_value=[{
        "url": "https://docs.example.com/release", "title": "Current official release",
        "snippet": "Current release limitations are documented here.",
    }]))
    monkeypatch.setattr(engine.search, "fetch_page_receipt", AsyncMock(return_value={
        "url": "https://docs.example.com/release", "title": "Current official release",
        "text": "The current official release documentation describes its limitations. " * 8,
        "truncated": False, "status": "retrieved",
    }))

    result = asyncio.run(engine.run_research(
        f"What are the current official release limitations? {query_sentinel}",
        checkpoint=AsyncMock(), progress=AsyncMock()))
    assert result["status"] == "partial"
    assert len(requests) == result["usage"]["model_calls"] == 2
    assert result["usage"]["failure_stage"] == "solver"
    assert result["usage"]["failure_provider"] == "gemini"
    assert result["usage"]["failure_category"] == "request_rejected"
    assert result["usage"]["failure_http_status"] == 400
    assert "stage=planner" in caplog.text and "stage=solver" in caplog.text
    assert "category=request_rejected http_status=400" in caplog.text
    assert "HTTP 400" in result["answer"]
    assert "independently checked conclusion" in result["answer"]
    fetched = [source for source in result["sources"] if source["method"] == "http"]
    assert len(fetched) == 1 and fetched[0]["content_hash"]
    assert fetched[0]["url"] == "https://docs.example.com/release"
    for forbidden in (query_sentinel, body_sentinel, "offline-test-key", "must-not-fall-back"):
        assert forbidden not in result["answer"] + caplog.text


@pytest.mark.parametrize("response_format", [None, {"type": "json_object"}])
def test_plain_and_json_object_calls_do_not_gain_a_schema(monkeypatch, response_format):
    def handler(request):
        generation = json.loads(request.content)["generationConfig"]
        assert "responseSchema" not in generation
        assert "responseJsonSchema" not in generation
        assert generation["maxOutputTokens"] == 4096
        if response_format:
            assert generation["responseMimeType"] == "application/json"
        else:
            assert "responseMimeType" not in generation
        return gemini_response({"ok": True})

    mock_http(monkeypatch, handler)
    asyncio.run(llm._call_gemini("policy", [{"role": "user", "content": "task"}],
                                "configured-primary", response_format))


def test_nonresearch_nullable_json_schema_is_preserved(monkeypatch):
    response_format = {"type": "json_schema", "json_schema": {
        "name": "task_completion", "schema": {"type": "object", "properties": {
            "completed_task_id": {"type": ["integer", "null"]}},
            "required": ["completed_task_id"]}}}

    def handler(request):
        generation = json.loads(request.content)["generationConfig"]
        assert generation["responseJsonSchema"] == response_format["json_schema"]["schema"]
        return gemini_response({"completed_task_id": None})

    mock_http(monkeypatch, handler)
    text, _, _ = asyncio.run(llm._call_gemini(
        "policy", [{"role": "user", "content": "task"}], "configured-primary", response_format))
    assert json.loads(text) == {"completed_task_id": None}


@pytest.mark.parametrize("final_parts, expected", [
    ([{"text": '{"queries":'}, {"text": '["official release"]}', "thought": False}],
     '{"queries":["official release"]}'),
    ([], ""),
])
def test_gemini_thought_parts_are_never_returned_as_answer(monkeypatch, final_parts, expected):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [
            {"text": "private-thought-sentinel", "thought": True}, *final_parts,
        ]}}],
        "usageMetadata": {"candidatesTokenCount": 40, "thoughtsTokenCount": 10},
    }))
    text, usage, _ = asyncio.run(llm._call_gemini(
        "policy", [{"role": "user", "content": "task"}], "configured-primary", engine._PLAN_SCHEMA))
    assert text == expected
    assert usage["completion_tokens"] == 50


def test_nonresearch_function_call_signature_roundtrip_is_preserved(monkeypatch):
    function_call = {"name": "lookup", "args": {"query": "official release"}}
    signature = "offline-thought-signature"
    tool = {"type": "function", "function": {
        "name": "lookup", "description": "Look up a public release",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    }}
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["tools"][0]["functionDeclarations"][0] == {
            "name": "lookup", "description": "Look up a public release",
            "parameters": tool["function"]["parameters"],
        }
        if len(calls) == 2:
            assert body["contents"][1]["parts"] == [{
                "functionCall": function_call, "thoughtSignature": signature,
            }]
            assert body["contents"][2]["parts"] == [{"functionResponse": {
                "name": "lookup", "response": {"result": "release result"},
            }}]
            return gemini_response({"ok": True})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"functionCall": function_call, "thoughtSignature": signature},
        ]}}]})

    mock_http(monkeypatch, handler)
    messages = [{"role": "user", "content": "task"}]
    _, _, returned_tools = asyncio.run(llm._call_gemini(
        "policy", messages, "configured-primary", tools=[tool]))
    assert returned_tools[0]["function"]["thoughtSignature"] == signature
    assert returned_tools[0]["function"]["_raw_fc"] == function_call
    messages += [{"role": "assistant", "content": "", "tool_calls": returned_tools},
                 {"role": "tool", "name": "lookup", "content": "release result"}]
    asyncio.run(llm._call_gemini("policy", messages, "configured-primary", tools=[tool]))
    assert len(calls) == 2


@pytest.mark.parametrize("provider", ["gemini", "groq", "openrouter"])
@pytest.mark.parametrize("status", [400, 429, 503])
def test_http_errors_keep_status_without_logging_response_content(monkeypatch, caplog, provider, status):
    sentinel = "private-prompt-and-upstream-response-sentinel"
    mock_http(monkeypatch, lambda request: httpx.Response(status, json={
        "error": {"message": sentinel, "details": [{"input": sentinel}]}}))
    caplog.set_level(logging.ERROR)
    messages = [{"role": "user", "content": sentinel}]
    if provider == "gemini":
        operation = llm._call_gemini("policy", messages, "configured-primary", engine._PLAN_SCHEMA)
    else:
        operation = llm._call_openai_compatible(
            provider, "https://provider.invalid", "offline-test-key", "policy", messages,
            "configured-primary", engine._PLAN_SCHEMA)
    with pytest.raises(httpx.HTTPStatusError) as error:
        asyncio.run(operation)
    assert error.value.response.status_code == status
    assert engine._transient(error.value) is (status in (429, 503))
    assert str(status) in caplog.text
    assert str(status) in str(error.value)
    assert sentinel not in caplog.text + str(error.value)
    assert "offline-test-key" not in caplog.text + str(error.value)
    assert "https://" not in str(error.value)


@pytest.mark.parametrize("payload", [
    {"promptFeedback": {"blockReason": "SAFETY", "blockReasonMessage": "private-content-sentinel"}},
    {"candidates": [{"finishReason": "SAFETY", "finishMessage": "private-content-sentinel"}]},
])
def test_blocked_candidate_does_not_put_content_in_exception(monkeypatch, payload):
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ValueError, match="invalid or blocked candidate") as error:
        asyncio.run(llm._call_gemini("policy", [{"role": "user", "content": "task"}],
                                    "configured-primary", engine._PLAN_SCHEMA))
    assert "private-content-sentinel" not in str(error.value)


def test_openai_compatible_wire_schema_and_budget_unchanged(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == engine._PLAN_SCHEMA
        assert body["model"] == "configured-primary"
        assert body["max_tokens"] == 1234
        assert "tools" not in body
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"queries":["official release"]}'}}], "usage": {}})

    mock_http(monkeypatch, handler)
    text, _, tools = asyncio.run(llm._call_openai_compatible(
        "groq", "https://provider.invalid", "offline-test-key", "policy",
        [{"role": "user", "content": "task"}], "configured-primary", engine._PLAN_SCHEMA,
        max_output_tokens=1234))
    assert json.loads(text) == {"queries": ["official release"]}
    assert tools == []
