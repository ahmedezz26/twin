"""Unit tests for tools.py"""

import dns.exception
import dns.name
import dns.resolver
import pytest

import tools
from tools import MAX_FIELD_LENGTH, _truncate


def test_truncate_keeps_short_text():
    # Arrange
    text = "Hello"
    # Act
    result = _truncate(text)
    # Assert
    assert result == "Hello"


def test_truncate_cuts_long_text_and_adds_ellipsis():
    long_text = "a" * (MAX_FIELD_LENGTH + 100)

    result = _truncate(long_text)

    assert result == "a" * MAX_FIELD_LENGTH + "…"


def test_truncate_strips_spaces_and_handles_none():
    assert _truncate("  hi  ") == "hi"
    assert _truncate(None) == ""


# --- Email format -------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    ["ezz@example.com", "first.last+tag@sub.example.de"],
)
def test_email_pattern_accepts_valid_addresses(email):
    assert tools.EMAIL_PATTERN.match(email)


@pytest.mark.parametrize(
    "email",
    ["", "no-at-sign.com", "two@@example.com", "spaces in@example.com", "missing@tld"],
)
def test_email_pattern_rejects_invalid_addresses(email):
    assert not tools.EMAIL_PATTERN.match(email)


# --- Email domain check (DNS is mocked: tests never do real lookups) --------------


class FakeMX:
    """Looks like one MX record from dnspython: only .exchange.to_text() is used."""

    def __init__(self, host):
        self.exchange = dns.name.from_text(host)


def fake_resolver(answers):
    """Build a stand-in for dns.resolver.resolve.

    `answers` maps a record type ("MX", "A", "AAAA") to what the lookup returns,
    or to an exception class that the lookup raises.
    """

    def resolve(domain, record_type, lifetime=None):
        result = answers[record_type]
        if isinstance(result, type) and issubclass(result, Exception):
            raise result()
        return result

    return resolve


def test_domain_with_mx_record_accepts_mail(monkeypatch):
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolver({"MX": [FakeMX("mail.example.com.")]}))
    assert tools.email_domain_accepts_mail("example.com") is True


def test_domain_with_null_mx_rejects_mail(monkeypatch):
    # RFC 7505: an MX record pointing to "." means "this domain accepts no email"
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolver({"MX": [FakeMX(".")]}))
    assert tools.email_domain_accepts_mail("example.com") is False


def test_domain_that_does_not_exist_rejects_mail(monkeypatch):
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolver({"MX": dns.resolver.NXDOMAIN}))
    assert tools.email_domain_accepts_mail("invented-domain.com") is False


def test_domain_without_mx_falls_back_to_address_record(monkeypatch):
    answers = {"MX": dns.resolver.NoAnswer, "A": ["1.2.3.4"]}
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolver(answers))
    assert tools.email_domain_accepts_mail("example.com") is True


def test_domain_without_any_record_rejects_mail(monkeypatch):
    answers = {"MX": dns.resolver.NoAnswer, "A": dns.resolver.NoAnswer, "AAAA": dns.resolver.NoAnswer}
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolver(answers))
    assert tools.email_domain_accepts_mail("example.com") is False


def test_dns_timeout_fails_open(monkeypatch):
    # If DNS itself is broken, accept the email: never lose a real lead because of our infrastructure
    monkeypatch.setattr(dns.resolver, "resolve", fake_resolver({"MX": dns.exception.Timeout}))
    assert tools.email_domain_accepts_mail("example.com") is True
