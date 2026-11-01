#!/usr/bin/env python3
"""
rsarchive.catalog.llm — the catalog's one doorway to Claude.

Two things live here and nothing else:

* :func:`make_client` — build an ``anthropic.Anthropic`` client. The SDK is an
  OPTIONAL dependency: it is imported only inside this function, so the catalog core
  (and every test) stays stdlib-only. A missing package or missing credentials raise
  :class:`LLMUnavailable` with instructions instead of an ``ImportError`` deep in a
  run.
* :func:`call_json` — send one request with a JSON-schema structured output
  (``output_config.format``) and return the parsed, schema-checked object. Refusals,
  truncation and malformed output raise :class:`LLMError` so a batch caller can count
  the failure and move on to the next lecture.

Every LLM entry point in the catalog (``tagging.propose_tags`` and friends) takes the
client as an argument, so tests pass a fake object exposing the same
``client.beta.messages.create(**kwargs)`` / ``client.messages.create(**kwargs)``
surface and nothing here ever touches the network on its own.

Model: ``claude-opus-5-5`` by default; override per call (``model=``) or for a whole
shell with ``RSARCHIVE_LLM_MODEL``. Claude Opus 5.5 always thinks adaptively (the
``thinking`` parameter is omitted, which is the supported form), depth is steered
with ``output_config.effort``, and requests opt into server-side refusal fallbacks
(``fallbacks: "default"``) so a classifier decline is retried on Anthropic's
recommended fallback model inside the same call.

Running with the SDK (it needs Python >= 3.10 for the current 1.x line)::

    uv run --python 3.12 --with anthropic python -m rsarchive.catalog.cli tags ...
    # or, in an existing environment:
    uv pip install anthropic

``from __future__ import annotations``; imports under Python 3.9.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ENV = "RSARCHIVE_LLM_MODEL"

# Server-side refusal fallback ("default" = Anthropic picks the fallback model by
# refusal category). The scalar form is gated by this exact beta header.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Non-streaming requests stay comfortably under the SDK's HTTP timeout at this size;
# a tag proposal is a few hundred output tokens plus adaptive thinking.
DEFAULT_MAX_TOKENS = 16000

INSTALL_HINT = (
    "The Anthropic Python SDK is required for LLM tagging but is not installed.\n"
    "  Install it into the environment you run from:   uv pip install anthropic\n"
    "  or run with inline dependencies (needs Python >= 3.10):\n"
    "    uv run --python 3.12 --with anthropic python -m rsarchive.catalog.cli tags ...\n"
)

KEY_HINT = (
    "No Anthropic credentials found. Either\n"
    "  export ANTHROPIC_API_KEY=sk-ant-...      (key from console.anthropic.com), or\n"
    "  ant auth login                           (stores a profile the SDK picks up).\n"
)


class LLMUnavailable(RuntimeError):
    """The SDK or credentials are missing (or the SDK is too old). Not retryable."""


class LLMError(RuntimeError):
    """One request failed: refusal, truncated output, or output that is not the JSON
    object the schema asked for. A batch caller counts it and continues."""


# ----------------------------------------------------------------------- client

def resolve_model(model: Optional[str] = None) -> str:
    """``model`` argument > ``$RSARCHIVE_LLM_MODEL`` > :data:`DEFAULT_MODEL`."""
    return (model or "").strip() or os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _has_credentials() -> bool:
    """True when the SDK can plausibly authenticate: an API key / auth token in the
    environment, Workload Identity Federation env vars, or an ``ant auth login``
    profile on disk (the SDK resolves all of these by itself)."""
    env = os.environ
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if env.get("ANTHROPIC_PROFILE") or env.get("ANTHROPIC_FEDERATION_RULE_ID"):
        return True
    cfg = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.isdir(os.path.join(cfg, "anthropic"))


def make_client(*, api_key: Optional[str] = None, check_credentials: bool = True) -> Any:
    """Return an ``anthropic.Anthropic`` client (deferred import).

    Raises :class:`LLMUnavailable` with install instructions when the package is
    missing, and with key instructions when no credential source is visible (pass
    ``api_key`` to inject one explicitly, or ``check_credentials=False`` to let the
    SDK decide)."""
    try:
        import anthropic  # type: ignore  # optional dependency
    except ImportError as exc:
        raise LLMUnavailable(INSTALL_HINT) from exc
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    if check_credentials and not _has_credentials():
        raise LLMUnavailable(KEY_HINT)
    return anthropic.Anthropic()


# ------------------------------------------------------------- schema checking

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, typ: str) -> bool:
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    py = _JSON_TYPES.get(typ)
    return True if py is None else isinstance(value, py)


def check_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Minimal structural check of ``value`` against the subset of JSON Schema the
    catalog's requests use (``type``, ``properties``, ``required``, ``items``,
    ``enum``). Returns a list of problems (empty = OK). Extra keys are tolerated —
    the caller's own validation decides what to keep."""
    problems: List[str] = []
    typ = schema.get("type")
    if isinstance(typ, list):
        if not any(_type_ok(value, t) for t in typ):
            return ["%s: expected one of %s" % (path, typ)]
    elif typ and not _type_ok(value, typ):
        return ["%s: expected %s, got %s" % (path, typ, type(value).__name__)]
    if "enum" in schema and value not in schema["enum"]:
        problems.append("%s: %r not in enum" % (path, value))
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                problems.append("%s: missing %r" % (path, key))
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems.extend(check_schema(value[key], sub, "%s.%s" % (path, key)))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            problems.extend(check_schema(item, schema["items"], "%s[%d]" % (path, i)))
    return problems


# --------------------------------------------------------------------- request

def build_request(
    *,
    system: str,
    user: str,
    schema: Dict[str, Any],
    model: Optional[str] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: Optional[str] = "high",
    cache_system: bool = True,
) -> Dict[str, Any]:
    """The keyword arguments for ``messages.create`` (without the fallback options).

    The system prompt is sent as one text block with ``cache_control`` so a batch
    over many lectures re-reads the stable instructions/taxonomy prefix from the
    prompt cache (a prefix below the model's minimum cacheable size simply isn't
    cached — harmless)."""
    sys_block: Dict[str, Any] = {"type": "text", "text": system}
    if cache_system:
        sys_block["cache_control"] = {"type": "ephemeral"}
    output_config: Dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    return {
        "model": resolve_model(model),
        "max_tokens": max_tokens,
        "system": [sys_block],
        "messages": [{"role": "user", "content": user}],
        "output_config": output_config,
    }


def _text_of(response: Any) -> str:
    """Concatenate the ``text`` blocks of a Messages response (thinking blocks and
    fallback markers are skipped)."""
    parts = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def call_json(
    client: Any,
    *,
    system: str,
    user: str,
    schema: Dict[str, Any],
    model: Optional[str] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: Optional[str] = "high",
    fallbacks: bool = True,
) -> Dict[str, Any]:
    """Send one structured-output request and return the parsed JSON object.

    ``client`` is an ``anthropic.Anthropic`` (see :func:`make_client`) or any fake
    with the same ``beta.messages.create`` / ``messages.create`` methods. With
    ``fallbacks`` (default) the request goes through the beta endpoint with
    ``fallbacks="default"``; otherwise the plain ``messages.create``.

    Raises :class:`LLMError` on a refusal (``stop_reason == "refusal"``), on
    ``max_tokens`` truncation, on output that is not JSON, or on JSON that fails
    :func:`check_schema`; :class:`LLMUnavailable` when the installed SDK does not
    accept the request parameters (too old)."""
    if client is None:
        raise LLMUnavailable("call_json needs a client (see llm.make_client)")
    kwargs = build_request(system=system, user=user, schema=schema, model=model,
                           max_tokens=max_tokens, effort=effort)
    try:
        if fallbacks:
            response = client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            response = client.messages.create(**kwargs)
    except TypeError as exc:
        # An SDK that predates output_config / fallbacks rejects the keywords.
        raise LLMUnavailable(
            "The installed anthropic SDK does not accept these request parameters "
            "(%s). Upgrade it: uv pip install -U anthropic" % exc) from exc

    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        raise LLMError("model declined the request (refusal, category=%s)" % category)
    if stop == "max_tokens":
        raise LLMError("output truncated at max_tokens=%d" % max_tokens)

    text = _text_of(response).strip()
    if not text:
        raise LLMError("response has no text block (stop_reason=%s)" % stop)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMError("response is not valid JSON: %s" % exc) from exc
    problems = check_schema(data, schema)
    if problems:
        raise LLMError("response does not match the schema: " + "; ".join(problems[:5]))
    return data
