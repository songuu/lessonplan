"""Content capture keeps useful lesson text while removing credentials and bounding JSON."""

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.observability.content import configured_secrets, sanitize_content


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def test_normal_chinese_messages_are_copied_without_changing_structure():
    messages = [{"role": "system", "content": "你是教师助手。"}, {"role": "user", "content": "讲解分数，举两个例子。"}]
    result, truncated = sanitize_content(messages, max_bytes=65536)
    assert result == messages
    assert not truncated
    messages[0]["content"] = "后来修改"
    assert result[0]["content"] == "你是教师助手。"


def test_nested_sensitive_fields_are_redacted_without_removing_normal_metrics():
    value = {"headers": {"Authorization": "secret-auth", "X-API-Key": "secret-api", "Cookie": "secret-cookie"},
             "llm_api_key": "secret-model", "clientSecret": "secret-client", "password": {"nested": "secret-pass"},
             "access_token": "secret-access", "bearer_token": "secret-bearer", "auth": "secret-auth-object",
             "token_count": 123, "max_tokens": 2048, "content": "正常回复"}
    result, truncated = sanitize_content(value, max_bytes=65536)
    serialized = encoded(result).decode()
    assert "secret-" not in serialized
    assert result["token_count"] == 123
    assert result["max_tokens"] == 2048
    assert result["content"] == "正常回复"
    assert not truncated


def test_common_api_key_and_private_key_formats_are_redacted_in_free_text():
    keys = ("AIzaSyExampleGoogleApiKey123456789012345", "AKIA1234567890ABCDEF", "ghp_abcdefghijklmnopqrstuv123456")
    pem = "-----BEGIN PRIVATE KEY-----\ncredentialbody\n-----END PRIVATE KEY-----"
    result, truncated = sanitize_content("正文 " + " ".join(keys) + " " + pem, max_bytes=65536)
    assert all(key not in result for key in keys)
    assert "credentialbody" not in result
    assert "正文" in result
    assert not truncated


def test_free_text_credentials_are_redacted_including_configured_secrets():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZWFjaGVyIn0.signature123"
    content = ("提示词保留。Bearer bearer-credential; api_key=assigned-key; password: \"quoted pass phrase\"; "
               f"jwt {jwt}; https://teacher:URLpass@localhost:3100/path?token=query-token "
               "sk-proj-abcdefghijklmnop pk-lf-abcdefghijklmnop configured-secret-key 结束。")
    result, truncated = sanitize_content(content, max_bytes=65536, secrets=("configured-secret-key",))
    for secret in ("bearer-credential", "assigned-key", "quoted pass phrase", jwt, "teacher", "URLpass",
                   "query-token", "sk-proj-abcdefghijklmnop", "pk-lf-abcdefghijklmnop", "configured-secret-key"):
        assert secret not in result
    assert "提示词保留" in result
    assert "结束" in result
    assert not truncated


def test_configured_secrets_extracts_known_credentials_including_secretstr_and_url_encoding():
    settings = SimpleNamespace(llm_api_key="model-secret", llm_fallback_api_key="fallback-secret",
        langfuse_secret_key=SecretStr("langfuse-secret"), langfuse_public_key="public-credential",
        jwt_secret="jwt-signing-secret", obs_id_hash_key=SecretStr("hash-secret"),
        cos_secret_key="cos-secret", cos_secret_id="cos-id", database_url="postgresql+asyncpg://dbuser:db%40pass@db/app",
        redis_url="redis://cacheuser:cache%2Fpass@cache:6379/0", llm_base_url="https://proxy:proxy-pass@llm/v1")
    result = configured_secrets(settings)
    for expected in ("model-secret", "fallback-secret", "langfuse-secret", "public-credential", "jwt-signing-secret",
                     "hash-secret", "cos-secret", "cos-id", "dbuser", "db@pass", "db%40pass", "cache/pass", "proxy-pass"):
        assert expected in result
    assert all(type(value) is str and value for value in result)
    assert len(result) == len(set(result))


@pytest.mark.parametrize("budget", [1, 2, 3, 4, 8, 32, 64, 128, 1024])
def test_utf8_and_escaped_characters_stay_within_the_exact_json_budget(budget):
    value = {"prompt": ('中文🙂\"\\\n' * 1000) + "Bearer should-never-leak"}
    result, truncated = sanitize_content(value, max_bytes=budget)
    assert len(encoded(result)) <= budget
    assert truncated
    assert "should-never-leak" not in encoded(result).decode()
    json.loads(encoded(result))


def test_secret_crossing_the_preview_boundary_is_redacted_before_truncation():
    secret = "credential-that-crosses-boundary"
    result, truncated = sanitize_content("中文" * 20 + secret + "正文" * 1000, max_bytes=160, secrets=(secret,))
    assert truncated
    assert "credential" not in encoded(result).decode()
    assert "crosses-boundary" not in encoded(result).decode()
    assert result["_truncated"] is True


def test_cycles_deep_trees_and_large_collections_are_bounded():
    cycle = {"text": "保留内容"}
    cycle["self"] = cycle
    deep = "叶子"
    for _ in range(100):
        deep = [deep]
    result, truncated = sanitize_content({"cycle": cycle, "deep": deep, "many": list(range(10000))}, max_bytes=65536)
    assert truncated
    assert result["cycle"]["text"] == "保留内容"
    assert len(result["many"]) < 1000
    assert len(encoded(result)) <= 65536


def test_unknown_objects_and_container_subclasses_do_not_execute_user_methods():
    class Hostile:
        def __str__(self):
            raise AssertionError("must not stringify unknown content")

        def __repr__(self):
            raise AssertionError("must not repr unknown content")

    class HostileDict(dict):
        def items(self):
            raise AssertionError("must not iterate arbitrary subclasses")

    result, truncated = sanitize_content({"object": Hostile(), "dict": HostileDict(), "invalid": {1: "omit key"},
                                         "nan": float("nan"), "huge": 10**10000}, max_bytes=65536)
    assert truncated
    assert encoded(result)


def test_redaction_also_applies_to_dictionary_keys_and_surrogate_text():
    result, truncated = sanitize_content({"configured-key": "正文\ud800", "token": "private"}, max_bytes=65536,
                                         secrets=("configured-key",))
    assert "configured-key" not in encoded(result).decode()
    assert "private" not in encoded(result).decode()
    assert "正文" in encoded(result).decode()
    assert not truncated


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization"])
@pytest.mark.parametrize("scheme", ["Basic", "Bearer", "Digest", "Negotiate"])
def test_authorization_header_removes_the_entire_credential(header, scheme):
    credential = "dXNlcjpzdXBlci1zZWNyZXQ="
    result, _ = sanitize_content(f"{header}: {scheme} {credential}\n正常正文", max_bytes=65536)
    assert credential not in result
    assert "正常正文" in result


def test_long_non_assignment_whitespace_does_not_block_capture():
    import time

    value = "password" + " " * 10000 + "not-an-assignment"
    started = time.monotonic()
    result, _ = sanitize_content(value, max_bytes=65536)
    assert result == value
    # A generous upper bound distinguishes the former quadratic regex from linear scanning.
    assert time.monotonic() - started < 1


def test_redaction_is_idempotent_across_capture_and_transport_boundaries():
    value = "Authorization: Basic credential\nBearer private-token; password=assigned-secret"
    first, _ = sanitize_content(value, max_bytes=65536)
    second, _ = sanitize_content(first, max_bytes=65536)
    assert first == second


@pytest.mark.parametrize("query", [
    "auth=UNCONFIGURED_AUTH_TEST", "secret=UNCONFIGURED_SECRET_TEST",
    "X-Amz-Credential=UNCONFIGURED_ACCESS_TEST&X-Amz-Signature=UNCONFIGURED_SIGNATURE_TEST",
    "api%5Fkey=UNCONFIGURED_KEY_TEST", "X-Amz-Security-Token=UNCONFIGURED_SESSION_TEST",
])
def test_sensitive_url_query_keys_are_decoded_before_redaction(query):
    result, _ = sanitize_content("参考 https://example.test/path?keep=topic&" + query, max_bytes=65536)
    assert "UNCONFIGURED" not in result
    assert "keep=topic" in result


def test_cookie_and_digest_headers_do_not_keep_trailing_credentials():
    value = ('Authorization: Digest username="teacher", response="UNCONFIGURED_RESPONSE"\n'
             'Cookie: teacher_session=UNCONFIGURED_SESSION; csrf=UNCONFIGURED_CSRF\n正文')
    result, _ = sanitize_content(value, max_bytes=65536)
    assert "UNCONFIGURED" not in result
    assert "正文" in result


def test_long_non_url_scheme_text_is_scanned_with_a_bounded_scheme_length():
    import time

    value = "a." * 20000
    started = time.monotonic()
    result, _ = sanitize_content(value, max_bytes=65536)
    assert result == value
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("secret, value", [
    ("pass", "password=UNCONFIGURED_VALUE_TEST"),
    ("token", "https://example.test/?token=UNCONFIGURED_VALUE_TEST"),
    ("Authorization", "Authorization: Basic UNCONFIGURED_VALUE_TEST"),
])
def test_known_secret_prefix_does_not_hide_the_structure_of_other_credentials(secret, value):
    result, _ = sanitize_content(value, max_bytes=65536, secrets=(secret,))
    assert "UNCONFIGURED" not in result
