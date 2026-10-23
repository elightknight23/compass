"""Pay the cold-start cost at boot instead of on the first user message.

The first request through each slow dependency costs seconds; every one
after it costs milliseconds. That is connection setup and model loading,
paid once per process — and without this it lands on whoever sends the
first message, which in a demo is the examiner.

Four things get checked or warmed, in the order a real turn touches them:

  1. the model client   — every message goes through the classifier
  2. the embedder       — loads the ONNX MiniLM weights into memory
  3. ChromaDB           — the collection handle and its segments
  4. index freshness    — a stale index warns here, loudly, instead of
                          quietly answering from last week's regulations

Run on a daemon thread so uvicorn binds its port immediately. A failure
here is logged and dropped: warmup is an optimisation, and a server that
refuses to start because it could not pre-warm would be a worse outcome
than a slow first message.
"""

from __future__ import annotations

import sys
import threading
import time


def _log(message: str) -> None:
    print(f"[compass] {message}", file=sys.stderr, flush=True)


def warm() -> None:
    """Touch every slow-to-initialise dependency once. Safe to call twice."""
    started = time.perf_counter()

    try:
        from . import llm

        _, provider = llm.complete(
            [{"role": "user", "content": "ok"}], max_tokens=1, temperature=0
        )
        _log(f"warm: model ready via {provider} ({(time.perf_counter() - started):.1f}s)")
    except Exception as e:  # noqa: BLE001 — never block startup
        _log(f"warm: model FAILED ({type(e).__name__}: {e})")

    mark = time.perf_counter()
    try:
        from . import rag

        rag.embed(["ok"])
        _log(f"warm: local embedder ready ({(time.perf_counter() - mark):.1f}s)")
    except Exception as e:  # noqa: BLE001
        _log(f"warm: embedder FAILED ({type(e).__name__}: {e})")

    mark = time.perf_counter()
    try:
        from . import rag

        status = rag.index_status()
        if status["state"] == "missing":
            _log("warm: NO REGULATIONS INDEX — run scripts/index_docs.py")
        else:
            # A real retrieval, not just opening the client: this pulls the
            # collection's segments into memory, which is the slow part.
            rag.retrieve("minimum attendance requirement", k=1)
            _log(f"warm: vector store ready ({(time.perf_counter() - mark):.1f}s)")
            if status["state"] == "stale":
                _log(
                    "warm: INDEX IS STALE — the regulations changed since they "
                    "were indexed, so answers would come from the old text. "
                    "Run scripts/index_docs.py."
                )
    except Exception as e:  # noqa: BLE001
        _log(f"warm: vector store FAILED ({type(e).__name__}: {e})")

    _log(f"warm: done in {(time.perf_counter() - started):.1f}s — Compass is ready")


def warm_in_background() -> threading.Thread:
    t = threading.Thread(target=warm, name="compass-warmup", daemon=True)
    t.start()
    return t
