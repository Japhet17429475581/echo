import pytest

from echo.catalog import identity
from echo.domain import Status, merge_key, normalize, safe_url, username


@pytest.mark.parametrize(
    "value,expected",
    [(" @echo_user ", "echo_user"), ("user.name", "user.name"), ("тест_ник", "тест_ник"), ("a-b", "a-b")],
)
def test_username(value, expected):
    assert username(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-flag",
        "--help",
        "a b",
        "https://example.com",
        "@@user",
        "a/../b",
        "a\nB",
        "=SUM(1)",
        "x" * 65,
        "a?x=1",
        ".",
        "..",
    ],
)
def test_invalid_username(value):
    with pytest.raises(ValueError):
        username(value)


@pytest.mark.parametrize(
    "code,raw,expected",
    [
        (403, "Claimed", Status.BLOCKED),
        (429, "Available", Status.RATE_LIMITED),
        (500, "Claimed", Status.SITE_ERROR),
        (503, "Available", Status.SITE_ERROR),
        (200, "Unknown", Status.UNKNOWN),
        (200, "Claimed", Status.POSSIBLE_MATCH),
        (200, "Available", Status.NOT_FOUND),
        (404, "Available", Status.NOT_FOUND),
        (404, "Unknown", Status.UNKNOWN),
        (200, "Illegal", Status.UNKNOWN),
    ],
)
def test_normalization_preserves_engine_rules(observation, code, raw, expected):
    assert normalize(observation.model_copy(update={"http_code": code, "raw_status": raw})) == expected


@pytest.mark.parametrize("kind", ["home", "login", "search"])
def test_redirect_downgrades_match(observation, kind):
    assert normalize(observation.model_copy(update={"redirect_kind": kind})) == Status.UNKNOWN


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "https://a.onion/u",
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://[::1]/a",
        "https://user:pass@example.com",
        "https://a.com\n/a",
    ],
)
def test_unsafe_urls(value):
    assert safe_url(value) is None


def test_dedup_does_not_destroy_case_or_query():
    assert safe_url("https://EXAMPLE.COM:443/A?x=1&x=2#fragment") == "https://example.com/A?x=1&x=2#fragment"
    assert merge_key("a", "https://example.com/#/one") != merge_key("a", "https://example.com/#/two")
    assert merge_key("a", "https://example.com/A?x=1") != merge_key("a", "https://example.com/a?x=1")
    assert merge_key("a", "https://example.com/A?x=1") != merge_key("a", "https://example.com/A?x=2")
    assert merge_key("a", "https://example.com/A") != merge_key("b", "https://example.com/A")


def test_psl_and_explicit_aliases():
    assert identity("one.github.io", "test", {}) != identity("two.github.io", "test", {})
    assert identity("one.blogspot.com", "test", {}) != identity("two.blogspot.com", "test", {})
    assert identity("reddit.com", "Reddit", {"reddit.com": "reddit", "old.reddit.com": "reddit"}) == "reddit"
    assert identity("codeberg.org", "codeberg", {}) == identity("codeberg.org", "Codeberg.org", {})
