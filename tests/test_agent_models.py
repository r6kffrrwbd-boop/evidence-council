"""Provider adapters: tool-call IDs and reasoning state survive round trips; keys stay out of URLs and logs.

All offline. The Gemini HTTP test talks to a fake Gemini server on 127.0.0.1, not to Google.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evidence_council.agent.budget import Budget, BudgetExceeded, Limits
from evidence_council.agent.messages import Message, ToolResult, ToolSpec
from evidence_council.agent.models import AnthropicModel, GeminiModel, OpenAIModel, build_model
from evidence_council.agent.models.gemini import gemini_schema
from evidence_council.config import DEFAULT_CONFIG, load_config
from evidence_council.errors import CouncilError
from evidence_council.providers.base import ProviderError

PRICING = DEFAULT_CONFIG["agent"]["pricing"]
TOOLS = [ToolSpec("read_file", "read", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
                                        "additionalProperties": False})]


def gemini(**kw):
    return GeminiModel({"model": "gemini-2.5-flash", "api_key_env": "EC_TEST_GEMINI_KEY", "base_url": kw.get("base_url", "http://x")}, PRICING, {})


GEMINI_REPLY = {
    "candidates": [{"content": {"role": "model", "parts": [
        {"text": "Reading the file.", "thoughtSignature": "SIG-TEXT"},
        {"functionCall": {"id": "call-abc", "name": "read_file", "args": {"path": "shop.py"}}, "thoughtSignature": "SIG-CALL"},
        {"text": "private summary", "thought": True}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50, "thoughtsTokenCount": 200},
}


def test_gemini_preserves_thought_signatures_and_call_ids():
    m = gemini()
    turn = m.from_wire(json.loads(json.dumps(GEMINI_REPLY)))
    assert turn.message.text == "Reading the file."          # thought part never becomes visible text
    assert turn.message.tool_calls[0].id == "call-abc"
    assert turn.usage.output_tokens == 250 and turn.usage.reasoning_tokens == 200
    convo = [Message.user("hi"), turn.message, Message.tool([ToolResult("call-abc", "read_file", "x = 1")])]
    body = m.to_wire("sys", convo, TOOLS)
    model_turn = body["contents"][1]
    assert model_turn == GEMINI_REPLY["candidates"][0]["content"]          # replayed verbatim, signatures included
    fr = body["contents"][2]["parts"][0]["functionResponse"]
    assert fr == {"name": "read_file", "response": {"output": "x = 1"}, "id": "call-abc"}
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    decl = body["tools"][0]["functionDeclarations"][0]
    assert "additionalProperties" not in decl["parameters"]                  # Gemini rejects it


def test_gemini_without_call_id_does_not_invent_one_on_the_wire():
    reply = {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a"}}}]}}]}
    m = gemini()
    turn = m.from_wire(reply)
    assert not turn.message.tool_calls[0].id_from_provider
    body = m.to_wire("", [Message.user("q"), turn.message, Message.tool([ToolResult(turn.message.tool_calls[0].id, "read_file", "r")])], [])
    assert "id" not in body["contents"][2]["parts"][0]["functionResponse"]


def test_gemini_schema_drops_unsupported_keywords():
    s = gemini_schema({"type": "object", "additionalProperties": False, "$schema": "x",
                       "properties": {"a": {"type": "array", "items": {"type": "string", "pattern": "^x"}}}})
    assert s == {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}}}}


def test_openai_preserves_encrypted_reasoning_and_call_id():
    m = OpenAIModel({"model": "some-model", "api_key_env": "X"}, PRICING, {})
    reply = {"output": [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENCRYPTED==", "summary": []},
        {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 3}}}
    turn = m.from_wire(reply)
    assert turn.message.tool_calls[0].id == "call_9" and turn.message.tool_calls[0].args == {"path": "a.py"}
    body = m.to_wire("sys", [Message.user("q"), turn.message, Message.tool([ToolResult("call_9", "read_file", "data")])], TOOLS)
    assert body["input"][1] == reply["output"][0] and body["input"][2] == reply["output"][1]
    assert body["input"][3] == {"type": "function_call_output", "call_id": "call_9", "output": "data"}
    assert body["store"] is False and "reasoning.encrypted_content" in body["include"]


def test_anthropic_preserves_thinking_signature_and_tool_use_id():
    m = AnthropicModel({"model": "some-model", "api_key_env": "X"}, PRICING, {})
    blocks = [{"type": "thinking", "thinking": "", "signature": "SIGNED"}, {"type": "redacted_thinking", "data": "OPAQUE"},
              {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}}]
    turn = m.from_wire({"content": blocks, "usage": {"input_tokens": 7, "output_tokens": 3}, "stop_reason": "tool_use"})
    assert turn.message.text == "" and turn.message.tool_calls[0].id == "toolu_1"
    body = m.to_wire("sys", [Message.user("q"), turn.message,
                             Message.tool([ToolResult("toolu_1", "read_file", "ok"), ToolResult("toolu_2", "x", "bad", True)])], TOOLS)
    assert body["messages"][1] == {"role": "assistant", "content": blocks}
    results = body["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"] and results[1]["is_error"] is True


def test_inline_api_key_in_config_is_refused(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"agent": {"providers": {"gemini": {"api_key": "AIzaSyD-abcdefghijklmnopqrstuvwxyz-fake01"}}}}))
    with pytest.raises(CouncilError) as e:
        load_config(p)
    assert "api_key_env" in e.value.hint


def test_build_model_uses_config_and_reports_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    m = build_model(DEFAULT_CONFIG["agent"])
    assert m.provider == "gemini"
    ok, why = m.check()
    assert not ok and "GEMINI_API_KEY" in why


def test_budget_refuses_a_call_that_could_exceed_the_spend_limit():
    m = gemini()
    b = Budget(Limits(max_cost_usd=0.001))
    with pytest.raises(BudgetExceeded):
        b.before_model_call(m.estimate_max_cost("s", [Message.user("x" * 100)], TOOLS))


def test_limits_can_only_be_lowered():
    lim = Limits(max_cost_usd=0.5, max_model_calls=30).lowered_by({"max_cost_usd": 5.0, "max_model_calls": 10})
    assert lim.max_cost_usd == 0.5 and lim.max_model_calls == 10


class _FakeGemini(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeGemini.seen.append({"path": self.path, "key": self.headers.get("x-goog-api-key"), "body": body})
        if len(_FakeGemini.seen) == 1:
            reply = GEMINI_REPLY
        elif len(_FakeGemini.seen) == 2:
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error": "slow down, key AIzaSyD-abcdefghijklmnopqrstuvwxyz-fake02"}')
            return
        else:
            reply = {"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_gemini_http_round_trip_key_in_header_signature_replayed_and_retry(monkeypatch):
    key = "AIzaSyD-abcdefghijklmnopqrstuvwxyz-fake03"
    monkeypatch.setenv("EC_TEST_GEMINI_KEY", key)
    _FakeGemini.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeGemini)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m = gemini(base_url=f"http://127.0.0.1:{srv.server_port}/v1beta")
        m.retries = 1
        t1 = m.complete("sys", [Message.user("go")], TOOLS)
        convo = [Message.user("go"), t1.message, Message.tool([ToolResult("call-abc", "read_file", "x")])]
        t2 = m.complete("sys", convo, TOOLS)          # first attempt gets HTTP 429, retried
        assert t2.message.text == "done"
    finally:
        srv.shutdown()
    first, retried = _FakeGemini.seen[0], _FakeGemini.seen[2]
    assert first["path"] == "/v1beta/models/gemini-2.5-flash:generateContent"
    assert key not in first["path"] and first["key"] == key          # header, never URL
    parts = retried["body"]["contents"][1]["parts"]
    assert [p.get("thoughtSignature") for p in parts[:2]] == ["SIG-TEXT", "SIG-CALL"]
    assert t1.cost_usd > 0


def test_provider_error_bodies_are_redacted():
    from evidence_council.util import redact_secrets
    msg = redact_secrets("HTTP 400: bad key AIzaSyD-abcdefghijklmnopqrstuvwxyz-fake02")[0]
    assert "AIza" not in msg
    with pytest.raises(ProviderError):
        gemini().from_wire({"promptFeedback": {"blockReason": "SAFETY"}})
