"""Model and credential configuration for Fairbill.

Reads .env from the repo root (never prints or logs any value) and hands back
configured Bedrock models. Primary is Sonnet 4.6; the ladder below it is Sonnet 4.5,
then Haiku 4.5.

Why a ladder and not a pair: Bedrock caps each model at a fixed number of tokens per
day per account (Sonnet 4.6: 10.8 M cross-region, not adjustable; measured 2026-09-14
when a day of benches spent 9.6 M and every call after 12:20 CT came back
"Too many tokens per day"). Each model id has its own daily bucket, so the second rung
is the same model family on an untouched bucket, and Haiku is the last rung, not the
first fallback. A rung that reports the daily cap is marked down for a while so the
next stage skips it instead of paying the retries again.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

from strands.event_loop._retry import ModelRetryStrategy

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

DEFAULT_LADDER = (
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
DEFAULT_PRIMARY = DEFAULT_LADDER[0]
DEFAULT_FALLBACK = DEFAULT_LADDER[-1]
DEFAULT_REGION = "us-east-1"
MODEL_NAMES = {
    "us.anthropic.claude-sonnet-4-6": "Claude Sonnet 4.6",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "Claude Sonnet 4.5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": "Claude Haiku 4.5",
}

# How long a rung stays out of the ladder after a throttle. The daily cap only clears
# when Bedrock's day rolls over, so it is long; a per-minute throttle clears in seconds.
DAILY_CAP_TTL_S = 900
THROTTLE_TTL_S = 60
_down: dict[str, tuple[float, str]] = {}
_down_lock = threading.Lock()

_loaded = False


def load_env() -> None:
    """Load .env once into os.environ. Values are never printed."""
    global _loaded
    if _loaded:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")
    _loaded = True


def region() -> str:
    load_env()
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def ladder() -> list[str]:
    """Every model id in order of preference. MODEL_LADDER (comma separated) overrides
    the default; MODEL_PRIMARY and MODEL_FALLBACK still pin the first and last rung."""
    load_env()
    raw = os.environ.get("MODEL_LADDER")
    rungs = [x.strip() for x in raw.split(",") if x.strip()] if raw else list(DEFAULT_LADDER)
    if os.environ.get("MODEL_PRIMARY"):
        rungs = [os.environ["MODEL_PRIMARY"]] + [r for r in rungs if r != os.environ["MODEL_PRIMARY"]]
    if os.environ.get("MODEL_FALLBACK"):
        rungs = [r for r in rungs if r != os.environ["MODEL_FALLBACK"]] + [os.environ["MODEL_FALLBACK"]]
    return rungs


def model_id(fallback: bool = False) -> str:
    rungs = ladder()
    return rungs[-1] if fallback else rungs[0]


def model_name(mid: str) -> str:
    return MODEL_NAMES.get(mid, mid)


def is_throttle(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return "Throttl" in text or "tokens per day" in text.lower()


def is_daily_cap(exc: BaseException) -> bool:
    return "tokens per day" in str(exc).lower()


def mark_down(mid: str, exc: BaseException) -> Optional[str]:
    """Take a rung out of the ladder after a throttle. Returns the plain-words reason
    the notes carry, or None when the error was not a throttle."""
    if not is_throttle(exc):
        return None
    daily = is_daily_cap(exc)
    reason = "hit its daily token limit on Bedrock" if daily else "was throttled by Bedrock"
    with _down_lock:
        _down[mid] = (time.time() + (DAILY_CAP_TTL_S if daily else THROTTLE_TTL_S), reason)
    return reason


def down_reason(mid: str) -> Optional[str]:
    with _down_lock:
        hit = _down.get(mid)
        if hit and hit[0] > time.time():
            return hit[1]
        _down.pop(mid, None)
    return None


def is_down(mid: str) -> bool:
    return down_reason(mid) is not None


def live_ladder() -> list[str]:
    """The ladder minus the rungs currently marked down; never empty."""
    rungs = ladder()
    live = [r for r in rungs if not is_down(r)]
    return live or rungs


def clear_down() -> None:
    with _down_lock:
        _down.clear()


def cache_enabled() -> bool:
    """Prompt caching is opt-in: FAIRBILL_CACHE=1 turns it on.

    Bedrock only caches a prefix of at least 1,024 tokens on Claude Sonnet (4,096 on
    Haiku) and drops it after 5 minutes idle. Measured on 2026-09-14: the reader system
    prompt is under that floor, so caching writes but never reads and costs a round of
    cacheWriteInputTokens for nothing. Left off by default; see
    _runs/2026-09-14_speed/exp/REPORT.md.
    """
    load_env()
    return (os.environ.get("FAIRBILL_CACHE") or "").strip().lower() in ("1", "true", "yes")


def make_model(fallback: bool = False, temperature: float = 0.0, max_tokens: int = 4096,
               cache: Optional[bool] = None, model: Optional[str] = None):
    """Return a configured BedrockModel. model= picks a rung by id; otherwise fallback=True
    selects the last rung (Haiku) and False the first (Sonnet 4.6).

    cache=None follows FAIRBILL_CACHE; cache=True/False forces prompt caching on or off.
    The boto client keeps one retry of its own: Strands' ModelRetryStrategy already
    retries a throttle three times, and botocore's default four on top of that turned one
    capped rung into 30 s of waiting (measured 2026-09-14, read stage 88 s).
    """
    load_env()
    from botocore.config import Config as BotocoreConfig
    from strands.models import BedrockModel
    kwargs: dict = {"boto_client_config": BotocoreConfig(retries={"max_attempts": 2, "mode": "standard"})}
    if cache_enabled() if cache is None else cache:
        from strands.models.bedrock import CacheConfig
        kwargs["cache_config"] = CacheConfig(strategy="auto")
        kwargs["cache_tools"] = "default"
    return BedrockModel(
        model_id=model or model_id(fallback),
        region_name=region(),
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )


class LadderRetry(ModelRetryStrategy):
    """Strands' backoff for a passing throttle, no backoff for the daily cap.

    A rung that has hit its daily token limit will not recover in the 2, 4, 8 s the retries
    wait, so retrying it costs the reader ~15 s per attempt for nothing; the exception goes
    straight back to the caller, which marks the rung down and moves to the next one.
    """

    def __init__(self):
        super().__init__(max_attempts=3, initial_delay=2, max_delay=8)

    def is_retryable(self, exception: Exception) -> bool:
        return super().is_retryable(exception) and not is_daily_cap(exception)


def retry_strategy() -> LadderRetry:
    """A fresh retry hook per agent (the strategy keeps an attempt counter)."""
    return LadderRetry()


_probed: dict[str, float] = {}


def probe(mid: str, timeout_s: float = 5.0) -> Optional[str]:
    """Ask Bedrock for one token from `mid` with no retries, once per process per rung.

    A rung that has hit its daily cap answers in under a second with the cap message; the
    reader then never starts a full vision pass on it (which cost 10 to 15 s of retries
    and a second attempt, measured 2026-09-14). A healthy rung costs one short call,
    about 1.3 s, which the reader hides under its image preparation. Returns the down
    reason, or None when the rung answered. Any other error is left to the real call.
    """
    with _down_lock:
        if time.time() - _probed.get(mid, 0.0) < DAILY_CAP_TTL_S:
            return down_reason(mid)
        _probed[mid] = time.time()
    load_env()
    import boto3
    from botocore.config import Config as BotocoreConfig
    client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                          config=BotocoreConfig(retries={"max_attempts": 1}, read_timeout=timeout_s,
                                                connect_timeout=3))
    try:
        client.converse(modelId=mid, messages=[{"role": "user", "content": [{"text": "hi"}]}],
                        inferenceConfig={"maxTokens": 1})
    except Exception as exc:  # noqa: BLE001 - only a throttle changes the ladder
        return mark_down(mid, exc)
    return None
