"""Optional Langfuse tracing.

Off unless LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST are set (and the `langfuse`
package is installed). Every helper is a cheap no-op when tracing is off, so the application, the
tests and the evaluation never depend on Langfuse being reachable.

One chat message becomes one trace: the routing call, every model call (prompt, reply, token
usage), every tool call, policy retrieval and confirmations appear as nested observations.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from app.core.config import get_settings

logger = logging.getLogger("app.tracing")

# One-time passwords and bearer tokens never leave the process.
_SECRETS = re.compile(
    r"(Temporary password:\s*)\S+|(\"?(?:password|new_password|current_password)\"?\s*[:=]\s*\"?)[^\"\s,}]+|(Bearer\s+)[A-Za-z0-9._-]+",
    re.I,
)


def _mask(*, data: Any, **_: Any) -> Any:
    if isinstance(data, str):
        return _SECRETS.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "[redacted]", data)
    if isinstance(data, dict):
        return {key: _mask(data=value) for key, value in data.items()}
    if isinstance(data, list):
        return [_mask(data=value) for value in data]
    return data


class _NoOp:
    """Stands in for a Langfuse observation when tracing is off."""

    def update(self, **_: Any) -> "_NoOp":
        return self


_NOOP = _NoOp()


@lru_cache
def client() -> Any | None:
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key and settings.langfuse_host):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning("langfuse_not_installed")
        return None
    try:
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            environment=settings.environment,
            mask=_mask,
        )
    except Exception:  # noqa: BLE001 - tracing must never stop the app
        logger.warning("langfuse_init_failed", exc_info=True)
        return None


def enabled() -> bool:
    return client() is not None


@contextmanager
def observe(name: str, *, as_type: str = "span", **fields: Any) -> Iterator[Any]:
    """Nested observation (span, generation, tool, retriever, agent, chain …) or a no-op."""
    langfuse = client()
    if langfuse is None:
        yield _NOOP
        return
    try:
        manager = langfuse.start_as_current_observation(name=name, as_type=as_type, **fields)
        observation = manager.__enter__()
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_observe_failed", exc_info=True)
        yield _NOOP
        return
    try:
        yield observation
    except BaseException as exc:
        try:
            observation.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:  # noqa: BLE001
            pass
        manager.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        manager.__exit__(None, None, None)


@contextmanager
def trace(name: str, *, user_id: str | None, session_id: str | None, tags: list[str] | None = None, **fields: Any) -> Iterator[Any]:
    """Root observation for one request, with user and session attached to the whole trace."""
    langfuse = client()
    if langfuse is None:
        yield _NOOP
        return
    try:
        from langfuse import propagate_attributes

        attributes = propagate_attributes(user_id=user_id, session_id=session_id, tags=tags, trace_name=name)
        attributes.__enter__()
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_trace_failed", exc_info=True)
        yield _NOOP
        return
    try:
        with observe(name, as_type="agent", **fields) as root:
            yield root
    finally:
        attributes.__exit__(None, None, None)


def safe_update(observation: Any, **fields: Any) -> None:
    try:
        observation.update(**fields)
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_update_failed", exc_info=True)


def flush() -> None:
    langfuse = client()
    if langfuse is not None:
        try:
            langfuse.flush()
        except Exception:  # noqa: BLE001
            pass
