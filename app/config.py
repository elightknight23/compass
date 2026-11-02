"""Configuration — environment variables and paths.

One module reads the environment; everything else imports named values from
here. `.env.example` documents every variable below, including the ones
whose defaults you should not need to change.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
REGULATIONS_DIR = DOCS_DIR / "regulations"  # the knowledge corpus
CHROMA_PATH = Path(os.getenv("CHROMA_PATH", str(ROOT / "data" / "chroma")))


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# -- the model ---------------------------------------------------------------
# Google AI Studio's free tier, over its OpenAI-compatible endpoint (see
# app/llm.py). Two models on the same key: each has its own free-tier quota,
# so when the primary is rate-limited or down the fallback answers. A blank
# fallback means no fallback.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")

# Read/write timeout, in seconds. Once a connection exists, a slow response
# is a real fault: these prompts answer in 2-4 seconds, and a model that has
# not answered in 12 is better abandoned for the fallback than waited on
# (30s was tried: a hung primary made one leave request take 46s). The
# SDK's own default is 600s, which is indistinguishable from hanging.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "12"))
LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", "15"))

# How long an idle connection is held open. httpcore's default is FIVE
# SECONDS — long enough to drop between two messages in a conversation, and
# every drop costs another handshake. Ten minutes covers a whole demo.
LLM_KEEPALIVE = float(os.getenv("LLM_KEEPALIVE", "600"))

# Retries inside the SDK, per provider. Zero on purpose: a 429 from the
# primary is answered faster by the fallback than by waiting out a backoff.
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "0"))

# -- the record store ---------------------------------------------------------
# postgres | json. Resolved once at startup by backends.get_backend(). JSON
# needs no setup at all, which is what makes it the disaster plan: if the
# database is unreachable during a demo, flip this and restart.
RECORD_BACKEND = os.getenv("RECORD_BACKEND", "json")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://compass:compass@localhost:5433/compass"
)

# -- the complaint tracker mirror (app/tracker.py) — off unless both are set ---
# GITHUB_REPO is "owner/name"; GITHUB_TOKEN a fine-grained token with
# Issues: read and write on that one repository, nothing else.


def repo_slug(value: str) -> str:
    """"owner/name" from that, or from the repo's URL as copied off GitHub."""
    slug = value.strip().removesuffix("/").removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/", "git@github.com:"):
        if slug.startswith(prefix):
            return slug[len(prefix):]
    return slug


GITHUB_REPO = repo_slug(os.getenv("GITHUB_REPO", ""))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
GITHUB_TIMEOUT = float(os.getenv("GITHUB_TIMEOUT", "10"))
TRACKER_RETRY_SECONDS = float(os.getenv("TRACKER_RETRY_SECONDS", "300"))

# -- the domain ---------------------------------------------------------------
# Defaults are the common Indian-university rule. They must match the
# college's actual regulations, or every attendance figure on screen is wrong.
ATTENDANCE_THRESHOLD = float(os.getenv("ATTENDANCE_THRESHOLD", "0.75"))
PERIODS_PER_DAY = int(os.getenv("PERIODS_PER_DAY", "6"))
OD_IS_CONDONED = _flag("OD_IS_CONDONED", True)

# -- uploaded documents --------------------------------------------------------
# Originals and their extracted text live here (gitignored); the registry is
# in the record backend and the vectors in ChromaDB.
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", str(DATA_DIR / "uploads")))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_PAGES = int(os.getenv("MAX_UPLOAD_PAGES", "400"))

# Medical certificates: stored on disk, shown to the student and to whoever
# decides the leave, never sent to a model.
CERTIFICATES_DIR = Path(os.getenv("CERTIFICATES_DIR", str(DATA_DIR / "certificates")))
MAX_CERTIFICATE_MB = int(os.getenv("MAX_CERTIFICATE_MB", "10"))
# Medical and family-emergency leave may be applied for after the fact, if
# the first day is no more than this many days ago. OD may not.
LEAVE_BACKDATE_DAYS = int(os.getenv("LEAVE_BACKDATE_DAYS", "7"))

# -- sessions -----------------------------------------------------------------
# Eight hours: a working day, so a demo never logs itself out mid-sentence.
SESSION_TTL = int(os.getenv("COMPASS_SESSION_TTL", str(8 * 60 * 60)))

_SECRET_FILE = DATA_DIR / ".session_secret"


def session_secret() -> bytes:
    """The HMAC key for session tokens.

    Read from COMPASS_SESSION_SECRET when set. Otherwise generated once and
    persisted beside the data, because a per-process random key would log
    everyone out on every `uvicorn --reload` — which during a demo looks
    exactly like a broken login.
    """
    from_env = os.getenv("COMPASS_SESSION_SECRET")
    if from_env:
        return from_env.encode()
    try:
        return _SECRET_FILE.read_bytes()
    except OSError:
        import secrets

        key = secrets.token_bytes(32)
        _SECRET_FILE.write_bytes(key)
        return key
