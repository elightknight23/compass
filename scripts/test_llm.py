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
    llm._cooling.clear()  # each case starts with no model cooling down
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
    install(lambda b: completion(b["model"], "ok"), fallback=f" {FALLBACK}, third-model ,{PRIMARY},,")
    check("a comma-separated list is several fallbacks, in order, without repeats or blanks",
          [p.name for p in llm.providers()] == [PRIMARY, FALLBACK, "third-model"])
    seen = install(lambda b: error(429) if b["model"] != "third-model" else completion(b["model"], "third"),
                   fallback=f"{FALLBACK},third-model")
    check("  and the chain goes on to the third when the first two are rate-limited",
          llm.complete([{"role": "user", "content": "hi"}]) == ("third", "third-model")
          and [b["model"] for b in seen] == [PRIMARY, FALLBACK, "third-model"])

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

    section("hedging: a SLOW primary gets company, the first answer wins")
    import time

    def slow_primary(delay: float):
        def handler(b):
            if b["model"] == PRIMARY:
                time.sleep(delay)
            return completion(b["model"], f"from {b['model']}")
        return handler

    config.LLM_HEDGE_SECONDS = 0.2
    seen = install(slow_primary(1.5))
    started = time.perf_counter()
    text, who = llm.complete([{"role": "user", "content": "hi"}])
    took = time.perf_counter() - started
    check("primary stalls 1.5s, hedge at 0.2s -> the fallback answers, and is named",
          (text, who) == (f"from {FALLBACK}", FALLBACK), f"{who} {text}")
    check("  in well under the stall", took < 1.0, f"{took:.2f}s")
    check("  both were asked, the primary first", [b["model"] for b in seen][:2] == [PRIMARY, FALLBACK], str(seen))

    def both_slow(b):
        time.sleep(0.4 if b["model"] == PRIMARY else 1.5)
        return completion(b["model"], f"from {b['model']}")

    install(both_slow)
    text, who = llm.complete([{"role": "user", "content": "hi"}])
    check("a primary that answers after the hedge but before the fallback still wins", who == PRIMARY, who)

    seen = install(lambda b: completion(b["model"], json.dumps({"word": "fast", "n": 1})))
    obj, who = llm.parse([{"role": "user", "content": "x"}], Pair)
    check("a prompt primary: no hedge, the fallback never asked", who == PRIMARY and len(seen) == 1, str(len(seen)))

    def slow_parse(b):
        if b["model"] == PRIMARY:
            time.sleep(1.5)
        return completion(b["model"], json.dumps({"word": b["model"], "n": 2}))

    install(slow_parse)
    obj, who = llm.parse([{"role": "user", "content": "x"}], Pair)
    check("parse() hedges the same way", (obj.word, who) == (FALLBACK, FALLBACK), f"{obj} {who}")

    install(lambda b: (time.sleep(0.4), error(503))[1] if b["model"] == PRIMARY else (time.sleep(0.6), error(503))[1])
    try:
        llm.complete([{"role": "user", "content": "hi"}])
        check("hedged and both fail -> one LLMUnavailable naming both", False)
    except llm.LLMUnavailable as e:
        check("hedged and both fail -> one LLMUnavailable naming both", PRIMARY in str(e) and FALLBACK in str(e), str(e))

    section("a budget for the whole call")
    config.LLM_HEDGE_SECONDS, config.LLM_BUDGET_SECONDS = 0.2, 0.8
    install(lambda b: (time.sleep(3), completion(b["model"], "too late"))[1], fallback=f"{FALLBACK},third-model")
    started = time.perf_counter()
    try:
        llm.complete([{"role": "user", "content": "hi"}])
        check("every model hanging -> LLMUnavailable once the budget is spent", False)
    except llm.LLMUnavailable as e:
        took = time.perf_counter() - started
        check("every model hanging -> LLMUnavailable once the budget is spent, not after every timeout",
              took < 1.5 and "no answer within 0.8s" in str(e), f"{took:.2f}s {e}")
    seen = install(lambda b: error(503) if b["model"] == PRIMARY else (time.sleep(0.5), completion(b["model"], "ok"))[1])
    config.LLM_HEDGE_SECONDS, config.LLM_BUDGET_SECONDS = 5, 0
    started = time.perf_counter()
    _, who = llm.complete([{"role": "user", "content": "hi"}])
    check("a fast failure hands over at once, not after the hedge (5s here)",
          who == FALLBACK and time.perf_counter() - started < 1.5, f"{time.perf_counter() - started:.2f}s")
    config.LLM_BUDGET_SECONDS = 15

    section("a rate-limited model waits its turn")
    config.LLM_HEDGE_SECONDS = 0
    llm._cooling.clear()
    seen = install(lambda b: error(429, "Quota exceeded. Please retry in 30.5s.") if b["model"] == PRIMARY
                   else completion(b["model"], "ok"))
    llm.complete([{"role": "user", "content": "hi"}])
    seen.clear()
    _, who = llm.complete([{"role": "user", "content": "hi"}])
    check("after a 429 that says 'retry in 30.5s', the next turn goes to the fallback first",
          who == FALLBACK and [b["model"] for b in seen] == [FALLBACK], str([b["model"] for b in seen]))
    check("  for no longer than it said (capped at a minute)",
          29 < llm._cooling[PRIMARY] - time.monotonic() <= 31)
    seen = install(lambda b: error(503) if b["model"] == FALLBACK else completion(b["model"], "primary after all"))
    llm._cooling[PRIMARY] = time.monotonic() + 30  # still cooling, as above
    check("  and the cooling model still answers if the others fail — moved to the back, not dropped",
          llm.complete([{"role": "user", "content": "hi"}]) == ("primary after all", PRIMARY)
          and [b["model"] for b in seen] == [FALLBACK, PRIMARY])
    llm._cooling.clear()

    seen = install(lambda b: error(503, "This model is currently experiencing high demand.")
                   if b["model"] == PRIMARY else completion(b["model"], "ok"))
    llm.complete([{"role": "user", "content": "hi"}])
    seen.clear()
    _, who = llm.complete([{"role": "user", "content": "hi"}])
    check("a 503 'high demand' names no wait, but the next turn still skips to the fallback",
          who == FALLBACK and [b["model"] for b in seen] == [FALLBACK], str([b["model"] for b in seen]))
    check("  briefly", 0 < llm._cooling[PRIMARY] - time.monotonic() <= llm.OVERLOADED_COOLDOWN_SECONDS)
    seen = install(lambda b: error(500) if b["model"] == PRIMARY else completion(b["model"], "ok"))
    llm.complete([{"role": "user", "content": "hi"}])
    check("  a plain 500 doesn't bench a model: it may be this request, not the model",
          PRIMARY not in llm._cooling)
    llm._cooling.clear()

    seen = install(slow_primary(0.5))
    text, who = llm.complete([{"role": "user", "content": "hi"}])
    check("LLM_HEDGE_SECONDS=0: one at a time — the slow primary is waited for", who == PRIMARY and len(seen) == 1)

    return finish("llm provider chain")


if __name__ == "__main__":
    raise SystemExit(main())
