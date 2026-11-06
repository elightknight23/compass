"""The model, behind one interface — and a fallback when the free tier runs out.

Compass must cost nothing to run, so it talks to Google AI Studio's free
tier over its OpenAI-compatible endpoint: a primary model, then a fallback
model on the same key. Each model has its own free-tier quota, so a 429 on
one is usually not a 429 on the other. The rest of the code never learns
which one answered — except the trace, which says so, because "the
classifier got this wrong" and "the *fallback* classifier got this wrong"
are different findings.

Two things are deliberate:

1. **One connection pool, held open.** Every provider client shares a single
   `httpx.Client` with a long keepalive. A client built per call throws the
   pool away and pays a TLS handshake every turn; httpcore's default
   keepalive of five seconds drops the connection between two messages of
   the same conversation. Both were measured costs in the predecessor.

2. **Fallback is for availability, not for disagreement.** The next
   provider is tried when the current one is rate-limited, down,
   unreachable, or rejects the credentials — never because an answer looked
   wrong. A system that re-asks until it likes the answer is not a
   classifier. A provider that is merely *slow* gets company: after
   LLM_HEDGE_SECONDS the next one is asked too and the first answer wins
   (`_first_answer`), so a stalled call costs seconds, not the timeout.

Structured output is requested natively first. If a provider rejects the
schema (OpenAI-compatible layers vary in how much of JSON Schema they
accept), the same request is retried once in JSON mode with the schema
stated in the prompt and the reply validated by Pydantic. Either way the
caller gets a validated model instance or an exception — never a dict that
merely looks right.
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import config

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str


class LLMUnavailable(RuntimeError):
    """Every configured provider failed. The message names each failure."""


class ProviderMisbehaved(RuntimeError):
    """A provider answered 200 with something that is not a chat completion.

    A retired endpoint does exactly this — the first provider this project
    used now replies a bare text/plain "OK" to every request, which the SDK
    hands back as a str. That is an availability failure, not a bug in the
    call.
    """


def providers() -> list[Provider]:
    """Configured providers, primary first, then each fallback in the order
    GEMINI_FALLBACK_MODEL lists them (comma-separated). Without a key the
    chain is empty; a blank or duplicate model is simply absent. A provider
    is named by its model id, which is what the trace shows."""
    if not config.GEMINI_API_KEY:
        return []
    models = [config.GEMINI_MODEL]
    for m in (config.GEMINI_FALLBACK_MODEL or "").split(","):
        m = m.strip()
        if m and m not in models:
            models.append(m)
    return [
        Provider(m, config.GEMINI_BASE_URL, config.GEMINI_API_KEY, m)
        for m in models
        if m
    ]


@lru_cache(maxsize=1)
def http_client():
    """The shared connection pool. One per process, kept warm."""
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(
            connect=config.LLM_CONNECT_TIMEOUT,
            read=config.LLM_TIMEOUT,
            write=config.LLM_TIMEOUT,
            pool=10.0,
        ),
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=config.LLM_KEEPALIVE,
        ),
    )


@lru_cache(maxsize=4)
def client(name: str):
    """The OpenAI-compatible client for one provider, cached per process."""
    from openai import OpenAI

    p = next((p for p in providers() if p.name == name), None)
    if p is None:
        raise LLMUnavailable(f"provider {name!r} is not configured")
    return OpenAI(
        base_url=p.base_url,
        api_key=p.api_key,
        max_retries=config.LLM_MAX_RETRIES,
        http_client=http_client(),
    )


def _is_availability_error(e: Exception) -> bool:
    """Errors that mean "try someone else", as opposed to a bug in the call."""
    import openai

    return isinstance(
        e,
        (
            openai.RateLimitError,
            openai.APIConnectionError,  # includes APITimeoutError
            openai.InternalServerError,
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.NotFoundError,  # model id not served by this provider
            ProviderMisbehaved,
        ),
    )


def _completion(p: Provider, call):
    """Run one SDK call; raise ProviderMisbehaved unless it returned a completion."""
    try:
        resp = call()
    except AttributeError as e:
        # .parse() post-processes the body itself and trips over a str first.
        raise ProviderMisbehaved(f"{p.name}: response was not a chat completion ({e})") from e
    if not hasattr(resp, "choices"):
        raise ProviderMisbehaved(
            f"{p.name}: response was not a chat completion ({str(resp)[:40]!r})"
        )
    return resp


def _log(message: str) -> None:
    # stderr, so it never interleaves with anything a script prints to stdout.
    print(f"[compass-llm] {message}", file=sys.stderr, flush=True)


def _require_providers() -> list[Provider]:
    chain = providers()
    if not chain:
        raise LLMUnavailable(
            "No model provider is configured. Set GEMINI_API_KEY in .env "
            "(see .env.example)."
        )
    return chain


def _parse_with(p: Provider, messages: list[dict], schema: type[T], temperature: float) -> T:
    import openai

    try:
        completion = _completion(p, lambda: client(p.name).chat.completions.parse(
            model=p.model,
            temperature=temperature,
            messages=messages,
            response_format=schema,
        ))
        parsed = completion.choices[0].message.parsed
        if parsed is not None:
            return parsed
        # A refusal or an empty parse: fall through to JSON mode once.
    except openai.BadRequestError as e:
        _log(f"{p.name}: native structured output rejected ({e}); retrying in JSON mode")

    instructions = (
        "Respond with ONE JSON object and nothing else. It must validate "
        "against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema())
    )
    completion = _completion(p, lambda: client(p.name).chat.completions.create(
        model=p.model,
        temperature=temperature,
        messages=[*messages, {"role": "system", "content": instructions}],
        response_format={"type": "json_object"},
    ))
    return schema.model_validate_json(completion.choices[0].message.content or "")


class _Unusable(Exception):
    """This provider can't answer this call; the next one should try."""


# A model that said "quota exceeded, retry in 40s" will say it again for
# 40s. Until then it is tried LAST rather than first — not dropped, so it
# still answers if everything else is down — and no turn pays its 429.
_cooling: dict[str, float] = {}
MAX_COOLDOWN_SECONDS = 60.0
# A 503 "high demand" names no wait, but it comes in spells: on a busy day
# the free-tier primary answers 503 to nearly every call, and without this
# every turn would pay that failure before reaching a model that answers.
OVERLOADED_COOLDOWN_SECONDS = 20.0


def _note_rate_limit(name: str, e: Exception) -> None:
    import openai

    if isinstance(e, openai.RateLimitError):
        m = re.search(r"retry in ([\d.]+)s", str(e))
        wait_for = min(float(m.group(1)) if m else 10.0, MAX_COOLDOWN_SECONDS)
    elif isinstance(e, openai.InternalServerError) and e.status_code == 503:
        wait_for = OVERLOADED_COOLDOWN_SECONDS
    else:
        return
    _cooling[name] = time.monotonic() + wait_for


def _in_preference_order() -> list[Provider]:
    now = time.monotonic()
    # sorted() is stable: the configured order holds within each group.
    return sorted(_require_providers(), key=lambda p: _cooling.get(p.name, 0) > now)


# Hedged attempts run here. Threads, not asyncio: the SDK client is
# synchronous, and an abandoned attempt simply finishes in the background.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


def _first_answer(attempts: list[tuple[str, Callable[[], R]]]) -> tuple[R, str]:
    """The first provider to answer, in order of preference.

    The next provider starts when the current one FAILS — the fallback rule,
    and at once — or when it is merely SLOW: after LLM_HEDGE_SECONDS without
    an answer the next one is asked too, and whichever answers first is
    used. A model that stalls for eight seconds then costs about four. A
    preferred provider that answers after the hedge started still wins if it
    is first. LLM_HEDGE_SECONDS=0 turns hedging off (strictly one at a time).

    The whole call has a budget, LLM_BUDGET_SECONDS: when the service is
    struggling everywhere, saying so after fifteen seconds beats saying so
    after every provider's own timeout in turn.

    An attempt raises _Unusable for "try someone else"; anything else is a
    bug in the call and is raised as it is.
    """
    hedge = config.LLM_HEDGE_SECONDS or None
    budget = config.LLM_BUDGET_SECONDS or None
    deadline = time.monotonic() + budget if budget else None
    queue = list(attempts)
    running: dict = {}
    failures: list[str] = []

    def start() -> None:
        name, fn = queue.pop(0)
        running[_POOL.submit(fn)] = name

    start()
    while running:
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            failures.append(f"no answer within {budget:g}s from {', '.join(running.values())}")
            _log(failures[-1])
            break
        hedge_now = hedge if queue else None
        waits = [w for w in (hedge_now, left) if w is not None]
        done, _ = wait(list(running), timeout=min(waits) if waits else None, return_when=FIRST_COMPLETED)
        if not done:
            if hedge_now is not None and (left is None or hedge_now < left):
                _log(f"{', '.join(running.values())} slow after {hedge:g}s; also asking {queue[0][0]}")
                start()
            continue
        for fut in done:
            name = running.pop(fut)
            try:
                return fut.result(), name
            except _Unusable as e:
                failures.append(f"{name}: {e}")
                _log(f"falling back after {failures[-1]}")
                if queue:
                    start()  # a failure hands over at once
    raise LLMUnavailable("; ".join(failures))


def _each_provider(call: Callable[[Provider], R]) -> tuple[R, str]:
    """Run `call` against the providers in preference order (_first_answer),
    sorting its failures into "try the next one" and genuine bugs."""
    def attempt(p: Provider) -> Callable[[], R]:
        def run() -> R:
            try:
                return call(p)
            except ValidationError as e:
                raise _Unusable(f"reply did not match the schema ({e.error_count()} errors)") from e
            except Exception as e:  # noqa: BLE001 — classified just below
                if not _is_availability_error(e):
                    raise
                _note_rate_limit(p.name, e)
                raise _Unusable(f"{type(e).__name__}: {e}") from e
        return run

    return _first_answer([(p.name, attempt(p)) for p in _in_preference_order()])


def parse(
    messages: list[dict], schema: type[T], *, temperature: float = 0
) -> tuple[T, str]:
    """Structured output: (validated instance, provider name).

    Raises LLMUnavailable when every provider failed. Callers already treat
    a model failure as "ask the user" rather than a crash, so one exception
    type is all they need to catch.
    """
    return _each_provider(lambda p: _parse_with(p, messages, schema, temperature))


def complete(
    messages: list[dict],
    *,
    temperature: float = 0,
    max_tokens: int | None = None,
) -> tuple[str, str]:
    """Free-text completion: (text, provider name). Same fallback rules."""
    def text(p: Provider) -> str:
        resp = _completion(p, lambda: client(p.name).chat.completions.create(
            model=p.model,
            temperature=temperature,
            messages=messages,
            **({"max_tokens": max_tokens} if max_tokens else {}),
        ))
        return (resp.choices[0].message.content or "").strip()

    return _each_provider(text)
