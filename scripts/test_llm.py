"""The provider chain, against a fake HTTP transport — no network.

What is pinned here is the fallback contract in app/llm.py:

- availability failures (429, 5xx, a 200 that is not a chat completion)
  move on to the fallback model, and the result names the model that
  actually answered;
- a provider that rejects the native schema is retried once in JSON mode
  before anyone falls back;
- a bug in the call itself (400 on a plain completion) is NOT papered over
  by asking someone else;
- every model down is one exception, LLMUnavailable, naming each failure.

    python scripts/test_llm.py
"""

from __future__ import annotations

import json

from _harness import check, finish, section

import httpx
from pydantic import BaseModel

from app import config, llm

PRIMARY, FALLBACK = "primary-model", "fallback-model"


class Pair(BaseModel):
    word: str
    n: int


def completion(model: str, content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
    })


def error(status: int, message: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


def install(handler, *, fallback: str | None = FALLBACK) -> list[dict]:
    """Point llm at a fake transport. Returns the log of requests it saw."""
    seen: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return handler(body)

    config.GEMINI_API_KEY = "test-key"
    config.GEMINI_MODEL = PRIMARY
    config.GEMINI_FALLBACK_MODEL = fallback
    llm.client.cache_clear()
    fake = httpx.Client(transport=httpx.MockTransport(record))
    llm.http_client = lambda: fake  # type: ignore[assignment]
    return seen


def main() -> int:
    section("the chain")
    config.GEMINI_API_KEY = None
    check("no key -> no providers", llm.providers() == [])
    try:
        llm.complete([{"role": "user", "content": "hi"}])
        check("no key -> LLMUnavailable", False)
    except llm.LLMUnavailable as e:
        check("no key -> LLMUnavailable that says which variable to set",
              "GEMINI_API_KEY" in str(e), str(e))

    install(lambda b: completion(b["model"], "ok"))
    check("primary first, then fallback, named by model id",
          [p.name for p in llm.providers()] == [PRIMARY, FALLBACK])
    install(lambda b: completion(b["model"], "ok"), fallback="")
    check("a blank fallback means no fallback", [p.name for p in llm.providers()] == [PRIMARY])
    install(lambda b: completion(b["model"], "ok"), fallback=PRIMARY)
    check("a fallback equal to the primary is not tried twice",
          [p.name for p in llm.providers()] == [PRIMARY])

    section("complete(): availability failures fall back")
    seen = install(lambda b: completion(b["model"], "hello"))
    text, who = llm.complete([{"role": "user", "content": "hi"}])
    check("healthy primary answers, fallback untouched",
          (text, who) == ("hello", PRIMARY) and len(seen) == 1)

    seen = install(lambda b: error(429) if b["model"] == PRIMARY else completion(b["model"], "from fallback"))
    text, who = llm.complete([{"role": "user", "content": "hi"}])
    check("429 on the primary -> the fallback answers and is named",
          (text, who) == ("from fallback", FALLBACK) and [b["model"] for b in seen] == [PRIMARY, FALLBACK])

    install(lambda b: error(503) if b["model"] == PRIMARY else completion(b["model"], "ok"))
    check("503 on the primary -> fallback", llm.complete([{"role": "user", "content": "hi"}])[1] == FALLBACK)

    install(lambda b: httpx.Response(200, text="OK", headers={"content-type": "text/plain"})
            if b["model"] == PRIMARY else completion(b["model"], "real answer"))
    text, who = llm.complete([{"role": "user", "content": "hi"}])
    check("a 200 that is not a chat completion (a retired endpoint) -> fallback, not a crash",
          (text, who) == ("real answer", FALLBACK))

    install(lambda b: error(400, "bad request"))
    try:
        llm.complete([{"role": "user", "content": "hi"}])
        check("a 400 on a plain completion is raised, not retried elsewhere", False)
    except llm.LLMUnavailable:
        check("a 400 on a plain completion is raised, not retried elsewhere", False, "fell back")
    except Exception as e:  # noqa: BLE001
        check("a 400 on a plain completion is raised, not retried elsewhere",
              type(e).__name__ == "BadRequestError", type(e).__name__)

    install(lambda b: error(503))
    try:
        llm.complete([{"role": "user", "content": "hi"}])
        check("every model down -> LLMUnavailable", False)
    except llm.LLMUnavailable as e:
        msg = str(e)
        check("every model down -> one LLMUnavailable naming both models",
              PRIMARY in msg and FALLBACK in msg, msg)

    section("parse(): structured output")
    seen = install(lambda b: completion(b["model"], json.dumps({"word": "hello", "n": 3})))
    obj, who = llm.parse([{"role": "user", "content": "x"}], Pair)
    check("native structured output -> a validated instance",
          isinstance(obj, Pair) and obj.n == 3 and who == PRIMARY)
    check("the native request carries the JSON schema",
          seen[0].get("response_format", {}).get("type") == "json_schema")

    def schema_rejected(b):
        if b.get("response_format", {}).get("type") == "json_schema":
            return error(400, "schema not supported")
        return completion(b["model"], json.dumps({"word": "json-mode", "n": 1}))

    seen = install(schema_rejected)
    obj, who = llm.parse([{"role": "user", "content": "x"}], Pair)
    check("schema rejected -> retried once in JSON mode on the SAME model",
          obj.word == "json-mode" and who == PRIMARY
          and [b.get("response_format", {}).get("type") for b in seen] == ["json_schema", "json_object"])
    check("the JSON-mode retry states the schema in the prompt",
          "JSON Schema" in seen[1]["messages"][-1]["content"])

    install(lambda b: completion(b["model"], json.dumps({"word": "x"}))
            if b["model"] == PRIMARY else completion(b["model"], json.dumps({"word": "y", "n": 2})))
    obj, who = llm.parse([{"role": "user", "content": "x"}], Pair)
    check("a reply that fails the schema -> fallback, never a half-valid dict",
          (obj.word, who) == ("y", FALLBACK))

    install(lambda b: httpx.Response(200, text="OK", headers={"content-type": "text/plain"})
            if b["model"] == PRIMARY else completion(b["model"], json.dumps({"word": "z", "n": 0})))
    obj, who = llm.parse([{"role": "user", "content": "x"}], Pair)
    check("parse() also survives a non-completion 200", (obj.word, who) == ("z", FALLBACK))

    install(lambda b: error(429))
    try:
        llm.parse([{"role": "user", "content": "x"}], Pair)
        check("parse with every model rate-limited -> LLMUnavailable", False)
    except llm.LLMUnavailable:
        check("parse with every model rate-limited -> LLMUnavailable", True)

    return finish("llm provider chain")


if __name__ == "__main__":
    raise SystemExit(main())
