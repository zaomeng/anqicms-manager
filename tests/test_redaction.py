from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote, quote_plus

from core.redaction import SecretRedactor, redact_text


def site(**kw):
    values = dict(jiekou="", mcp_token="", admin_password="")
    values.update(kw)
    return SimpleNamespace(**values)


def test_empty_and_none_text():
    assert redact_text("") == ""
    assert redact_text(None) == ""
    assert redact_text("plain text without secrets") == "plain text without secrets"


def test_secret_replacement_and_min_length():
    assert redact_text("failed with supersecret!", ["supersecret"]) == "failed with ***!"
    # shorter than 4 chars is ignored (avoids shredding ordinary text)
    assert redact_text("abc is fine", ["abc"]) == "abc is fine"
    assert redact_text("x", ["", None, 123]) == "x"
    assert redact_text("tok1 tok1", ["  tok1  "]) == "*** ***"


def test_secret_url_encoded_variants_masked():
    secret = "a+b/c=d e"
    text = f"q={quote_plus(secret)} p={quote(secret, safe='')} raw={secret}"
    result = redact_text(text, [secret])
    assert "a%2Bb" not in result and "a+b/c" not in result
    assert "q=***" in result


def test_longer_secret_replaced_before_shorter_overlap():
    result = redact_text("value=abcd1234", ["abcd", "abcd1234"])
    assert "1234" not in result


def test_query_parameters_masked():
    text = (
        "GET https://x.com/api?token=SECRET1&api_key=SECRET2&ticket=SECRET3"
        "&password=SECRET4&apikey=SECRET5&page=2 failed"
    )
    result = redact_text(text)
    for leaked in ("SECRET1", "SECRET2", "SECRET3", "SECRET4", "SECRET5"):
        assert leaked not in result
    assert "page=2" in result and result.endswith("failed")


def test_query_key_case_insensitive_and_prefixed_names():
    result = redact_text("Token=ABC csrf_token=DEF mcp_token=GHI API-KEY=JKL")
    for leaked in ("ABC", "DEF", "GHI", "JKL"):
        assert leaked not in result


def test_bearer_header_masked():
    result = redact_text("Authorization: Bearer abc.DEF-123_xyz~+/= done")
    assert "abc.DEF" not in result
    assert "Bearer ***" in result and result.endswith("done")
    assert "bearer ***" in redact_text("bearer lower123")


def test_json_fields_masked():
    text = '{"user_name": "admin", "password": "p@ss\\"w0rd", "token": "tk999", "ticket":"t1", "n": 1}'
    result = redact_text(text)
    assert "p@ss" not in result and "tk999" not in result and '"t1"' not in result
    assert '"user_name": "admin"' in result
    assert '"password": "***"' in result


def test_url_userinfo_masked():
    result = redact_text("proxy https://user:pw@host.com/x and http://tok@h.com")
    assert "user:pw" not in result and "tok@" not in result
    assert "https://***@host.com/x" in result


def test_non_secret_text_untouched_by_patterns():
    text = "tokens are great; ticketing system; passwords: none"
    assert redact_text(text) == text


def test_redaction_is_idempotent():
    once = redact_text("token=SECRETVAL Bearer abcdef", ["SECRETVAL"])
    assert redact_text(once, ["SECRETVAL"]) == once


def test_redactor_basic_add_and_call():
    redactor = SecretRedactor()
    assert redactor("hello supersecret") == "hello supersecret"
    redactor.add("supersecret", "ab", "")
    assert redactor("hello supersecret ab") == "hello *** ab"
    assert "supersecret" not in repr(redactor)


def test_redactor_constructor_secrets():
    assert SecretRedactor(["mysecret1"])("x mysecret1") == "x ***"


def test_redactor_from_sites_collects_all_secret_kinds():
    sites = [
        site(
            jiekou="https://a.com/archive?token=importTok1",
            mcp_token="mcpTok22",
            admin_password="adminPw33",
        ),
        site(jiekou="https://b.com/archive?token=importTok4"),
        site(),
    ]
    redactor = SecretRedactor.from_sites(sites)
    text = "importTok1 mcpTok22 adminPw33 importTok4 harmless"
    result = redactor(text)
    assert result == "*** *** *** *** harmless"


def test_redactor_from_sites_handles_none_and_missing_attrs():
    assert SecretRedactor.from_sites(None)("abc") == "abc"
    assert SecretRedactor.from_sites([SimpleNamespace()])("abc") == "abc"
