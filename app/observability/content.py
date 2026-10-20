"""Bounded, credential-redacted JSON snapshots for explicitly enabled content capture."""

import json
import math
import re
from typing import Any
from urllib.parse import quote, unquote, unquote_plus, urlsplit, urlunsplit

from pydantic import SecretStr

_REDACTED = "[REDACTED]"
_OMITTED = "[OMITTED]"
_MAX_DEPTH = 12
_MAX_NODES = 2048
_MAX_ITEMS = 128
_SENSITIVE_NAMES = {
    "auth", "authentication", "authorization", "proxyauthorization", "apikey", "xapikey", "password", "passwd", "pwd", "secret",
    "clientsecret", "accesstoken", "refreshtoken", "idtoken", "token", "jwt", "cookie", "setcookie",
    "privatekey", "credential", "credentials", "signature", "sessionid", "sessiontoken",
}
_ASSIGNMENT_NAME = (
    r"(?:api[_ -]?key|x-api-key|access[_ -]?token|refresh[_ -]?token|id[_ -]?token|token|jwt|password|passwd|"
    r"client[_ -]?secret|secret(?:[_ -]?key)?|credential|signature|auth|authorization|cookie)"
)
_QUOTED_ASSIGNMENTS = (
    re.compile(r'(?i)(\b' + _ASSIGNMENT_NAME + r'\s*+["\']?\s*+[:=]\s*+)"[^"\r\n]*"'),
    re.compile(r"(?i)(\b" + _ASSIGNMENT_NAME + r"\s*+[\"']?\s*+[:=]\s*+)'[^'\r\n]*'"),
)
_PLAIN_ASSIGNMENT = re.compile(
    r"(?i)(\b" + _ASSIGNMENT_NAME + r"\s*+[\"']?\s*+[:=]\s*+)(?!\[REDACTED\])[^\s\"'&,;<>}\]]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s++(?!\[REDACTED\])[^\s\"',;<>}\]]+")
_HEADER_LINE = re.compile(
    r"(?im)(\b(?:proxy[-_ ]?authorization|authorization|(?:set[-_ ]?)?cookie)\s*+[\"']?\s*+[:=]\s*+)[^\r\n]*"
)
_URL = re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://[^\s<>\"']+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_PROVIDER_KEY = re.compile(r"\b(?:sk|pk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_-]{8,}\b")
_OTHER_API_KEY = re.compile(
    r"\b(?:AIza[0-9A-Za-z_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|$)", re.DOTALL)
_URL_CREDENTIALS = re.compile(r"(?i)((?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://)[^\s/@]+@")


def configured_secrets(settings: Any) -> tuple[str, ...]:
    """Read only known credential fields, including encoded URL credentials; never stringify objects."""
    values: set[str] = set()
    for name in (
        "llm_api_key", "llm_fallback_api_key", "langfuse_public_key", "langfuse_secret_key", "jwt_secret",
        "obs_id_hash_key", "cos_secret_id", "cos_secret_key",
    ):
        try:
            value = getattr(settings, name, "")
            if type(value) is SecretStr:
                value = value.get_secret_value()
            if type(value) is str and value:
                values.add(value)
        except Exception:
            # Telemetry must remain optional when a credential source is unavailable.
            continue
    for name in ("database_url", "redis_url", "llm_base_url", "llm_fallback_base_url", "langfuse_base_url"):
        try:
            value = getattr(settings, name, "")
            if type(value) is not str or not value:
                continue
            parsed = urlsplit(value)
            for credential in (parsed.username, parsed.password):
                if credential:
                    values.update((credential, unquote(credential)))
        except Exception:
            continue
    # A configured secret may appear percent-encoded in an error message or request URL.
    values.update(quote(value, safe="") for value in tuple(values))
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        normalized in _SENSITIVE_NAMES
        or any(part in normalized for part in ("password", "secret", "credential", "privatekey"))
        or normalized.endswith(("apikey", "token", "signature", "authorization", "cookie"))
    )


def _redact_url(match: re.Match) -> str:
    try:
        parsed = urlsplit(match[0])
        parts = []
        for item in parsed.query.split("&"):
            key = item.partition("=")[0]
            parts.append(key + "=" + _REDACTED if _sensitive_key(unquote_plus(key)) else item)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(parts), parsed.fragment))
    except ValueError:
        return _REDACTED


def _redactor(secrets: tuple[str, ...]):
    known = [secret for secret in secrets if type(secret) is str and secret]
    pattern = re.compile("|".join(re.escape(secret) for secret in sorted(set(known), key=len, reverse=True))) if known else None

    def redact(value: str) -> str:
        value = _URL.sub(_redact_url, value)
        value = _URL_CREDENTIALS.sub(lambda match: match[1] + _REDACTED + "@", value)
        value = _HEADER_LINE.sub(lambda match: match[1] + _REDACTED, value)
        value = _BEARER.sub("Bearer " + _REDACTED, value)
        value = _JWT.sub(_REDACTED, value)
        value = _PROVIDER_KEY.sub(_REDACTED, value)
        value = _OTHER_API_KEY.sub(_REDACTED, value)
        value = _PRIVATE_KEY.sub(_REDACTED, value)
        for assignment in _QUOTED_ASSIGNMENTS:
            value = assignment.sub(lambda match: match[1] + '"' + _REDACTED + '"', value)
        value = _PLAIN_ASSIGNMENT.sub(lambda match: match[1] + _REDACTED, value)
        if pattern is not None:
            # Preserve credential labels until structural rules have consumed their values.
            value = _REDACTED.join(pattern.sub(lambda _: _REDACTED, part) for part in value.split(_REDACTED))
        # Lone surrogates are not valid UTF-8, but must not break a business request.
        return value.encode("utf-8", errors="replace").decode("utf-8")

    return redact


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _fit_preview(value: Any, max_bytes: int) -> Any:
    preview = value if type(value) is str else _encode(value)
    candidate = {"_truncated": True, "preview": ""}
    if len(_encode(candidate).encode("utf-8")) > max_bytes:
        for fallback in ("[TRUNCATED]", "…", 0):
            if len(_encode(fallback).encode("utf-8")) <= max_bytes:
                return fallback
        return None
    lower, upper = 0, len(preview)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        candidate["preview"] = preview[:middle]
        if len(_encode(candidate).encode("utf-8")) <= max_bytes:
            lower = middle
        else:
            upper = middle - 1
    candidate["preview"] = preview[:lower]
    return candidate


def sanitize_content(value: Any, *, max_bytes: int, secrets: tuple[str, ...] = ()) -> tuple[Any, bool]:
    """Copy built-in JSON values, redact before clipping, and fit the encoded per-field budget.

    The boolean reports omitted or truncated content, not ordinary credential redaction. Invalid
    budgets return a null snapshot; valid positive budgets always produce a fitting JSON value.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        return None, True
    truncated = False
    nodes = 0
    ancestors: set[int] = set()
    try:
        redact = _redactor(secrets if type(secrets) is tuple else ())

        def walk(item: Any, depth: int = 0) -> Any:
            nonlocal truncated, nodes
            nodes += 1
            if nodes > _MAX_NODES or depth > _MAX_DEPTH:
                truncated = True
                return _OMITTED
            kind = type(item)
            if item is None or kind is bool:
                return item
            if kind is str:
                text = redact(item)
                if len(text) > max_bytes:
                    truncated = True
                    return text[:max_bytes] + "[TRUNCATED]"
                return text
            if kind is int:
                if item.bit_length() <= 1024:
                    return item
            elif kind is float:
                if math.isfinite(item):
                    return item
            elif kind in (dict, list, tuple):
                identity = id(item)
                if identity in ancestors:
                    truncated = True
                    return _OMITTED
                ancestors.add(identity)
                try:
                    if kind is dict:
                        result = {}
                        for index, (key, child) in enumerate(item.items()):
                            if index >= _MAX_ITEMS or nodes >= _MAX_NODES:
                                truncated = True
                                result["_truncated"] = True
                                break
                            if type(key) is not str:
                                truncated = True
                                continue
                            safe_key = redact(key)
                            if len(safe_key) > max_bytes:
                                safe_key = safe_key[:max_bytes] + "[TRUNCATED]"
                                truncated = True
                            result[safe_key] = _REDACTED if _sensitive_key(key) else walk(child, depth + 1)
                        return result
                    result = []
                    for index, child in enumerate(item):
                        if index >= _MAX_ITEMS or nodes >= _MAX_NODES:
                            truncated = True
                            result.append(_OMITTED)
                            break
                        result.append(walk(child, depth + 1))
                    return result
                finally:
                    ancestors.remove(identity)
            truncated = True
            return _OMITTED

        snapshot = walk(value)
        if len(_encode(snapshot).encode("utf-8")) > max_bytes:
            return _fit_preview(snapshot, max_bytes), True
        return snapshot, truncated
    except Exception:
        # Do not include the exception or the original object: either could reveal credentials.
        return _fit_preview(_OMITTED, max_bytes), True
