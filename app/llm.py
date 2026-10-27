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
   classifier.

Structured output is requested natively first. If a provider rejects the
schema (OpenAI-compatible layers vary in how much of JSON Schema they
accept), the same request is retried once in JSON mode with the schema
stated in the prompt and the reply validated by Pydantic. Either way the
caller gets a validated model instance or an exception — never a dict that
merely looks right.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from . import config

T = TypeVar("T", bound=BaseModel)


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
    """Configured providers, primary first. Without a key the chain is
    empty; a blank or duplicate fallback model is simply absent. A provider
    is named by its model id, which is what the trace shows."""
    if not config.GEMINI_API_KEY:
        return []
    models = [config.GEMINI_MODEL]
    if config.GEMINI_FALLBACK_MODEL and config.GEMINI_FALLBACK_MODEL not in models:
        models.append(config.GEMINI_FALLBACK_MODEL)
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


def parse(
    messages: list[dict], schema: type[T], *, temperature: float = 0
) -> tuple[T, str]:
    """Structured output: (validated instance, provider name).

    Raises LLMUnavailable when every provider failed. Callers already treat
    a model failure as "ask the user" rather than a crash, so one exception
    type is all they need to catch.
    """
    failures: list[str] = []
    for p in _require_providers():
        try:
            return _parse_with(p, messages, schema, temperature), p.name
        except ValidationError as e:
            failures.append(f"{p.name}: reply did not match the schema ({e.error_count()} errors)")
        except Exception as e:  # noqa: BLE001 — classified just below
            if not _is_availability_error(e):
                raise
            failures.append(f"{p.name}: {type(e).__name__}: {e}")
        _log(f"falling back after {failures[-1]}")
    raise LLMUnavailable("; ".join(failures))


def complete(
    messages: list[dict],
    *,
    temperature: float = 0,
    max_tokens: int | None = None,
) -> tuple[str, str]:
    """Free-text completion: (text, provider name). Same fallback rules."""
    failures: list[str] = []
    for p in _require_providers():
        try:
            resp = _completion(p, lambda: client(p.name).chat.completions.create(
                model=p.model,
                temperature=temperature,
                messages=messages,
                **({"max_tokens": max_tokens} if max_tokens else {}),
            ))
            return (resp.choices[0].message.content or "").strip(), p.name
        except Exception as e:  # noqa: BLE001
            if not _is_availability_error(e):
                raise
            failures.append(f"{p.name}: {type(e).__name__}: {e}")
            _log(f"falling back after {failures[-1]}")
    raise LLMUnavailable("; ".join(failures))
