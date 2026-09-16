from __future__ import annotations

import re

import pytest

from harness.egress_redact import (
    REDACTED,
    redact_egress,
    redact_egress_text,
    secret_kind_names,
)


SAMPLES = {
    "jwt": "header eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.signaturepadxx",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK8=\n-----END RSA PRIVATE KEY-----",
    "basic_auth_url": "https://user:hunter2@example.com/x",
    "postgres_url": "postgres://alice:pw@db.internal:5432/app",
    "mysql_url": "mysql://alice:pw@db.internal:3306/app",
    "mongodb_url": "mongodb+srv://alice:pw@cluster.mongodb.net/app",
    "redis_url": "redis://:pw@cache:6379/0",
    "amqp_url": "amqps://alice:pw@mq.internal/vhost",
    "sk_ant": "sk-ant-api03-abcdefghijklmnopqrstuv",
    "sk_star": "sk-abcdefghijklmnopqrstuvwx",
    "stripe_live": "sk_live_51AbCdEfGhIjKlMnOp",
    "google_api": "AIzaSyA-thisisafakegooglekey12",
    "github_pat": "github_pat_11AAAAAAAAAAAAAABBBBBBBBBB",
    "npm": "npm_abcdefghijklmnopqrstuvwx",
    "slack": "xox" + "b-00fixture-notasecrettoken",
    "aws_access": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret": "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}


def test_every_secret_kind_is_redacted():
    assert set(SAMPLES) == set(secret_kind_names())
    for kind, seeded in SAMPLES.items():
        out = redact_egress_text("keep %s" % seeded)
        assert REDACTED in out, kind
        assert seeded not in out, kind
        assert out.startswith("keep ")


def test_nested_payload_and_regex_error_fail_closed(monkeypatch):
    payload = redact_egress({"note": "token " + SAMPLES["sk_star"], "rows": [SAMPLES["jwt"]]})
    assert payload["note"] == "token " + REDACTED
    assert payload["rows"] == ["header " + REDACTED]
    assert SAMPLES["jwt"] not in str(payload)

    from harness import egress_redact as mod

    class _Boom:
        def sub(self, *a, **k):
            raise re.error("broken")

    monkeypatch.setattr(mod, "SECRET_KINDS", [("jwt", _Boom())] + list(mod.SECRET_KINDS[1:]))
    assert redact_egress_text("anything " + SAMPLES["jwt"]) == REDACTED
