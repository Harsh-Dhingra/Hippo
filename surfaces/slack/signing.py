"""Proving a request really came from Slack.

Everything else in this surface rests on this file. A Slack webhook endpoint is
a URL on the public internet that will approve a write into Jira if you ask it
nicely, so without signature verification it is not a surface, it is a hole.

Slack signs each request with an HMAC over the version, the timestamp and the
raw body, using a secret only the workspace and this install share. Three
things have to be right and each has been got wrong in public before:

**The raw body, not the parsed one.** Re-serialising a form and hashing that
produces a different string from what Slack signed — a different field order,
a different encoding of a space — and the failure is intermittent rather than
total, which is worse. The verifier takes bytes.

**A timestamp window.** Without one, a signature captured from a log or a proxy
is valid forever, and replaying an approval is exactly the attack this
endpoint is worth attacking for.

**A constant-time comparison.** `==` on a signature leaks how much of it
matched through timing, which is enough to reconstruct one given patience.

Absent a signing secret the surface does not run. There is no development mode
that skips verification, because that mode is the one that reaches production.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

LOG = logging.getLogger("hippo.surfaces.slack.signing")

VERSION = "v0"

# Slack's own recommendation. Long enough for a slow network, short enough that
# a signature lifted from a log is not a standing key.
MAX_SKEW_SECONDS = 60 * 5


class SignatureError(Exception):
    """The request did not come from Slack, or did not come recently."""


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """What Slack would have sent for this request."""
    basestring = b"%s:%s:%s" % (VERSION.encode(), timestamp.encode(), body)
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"{VERSION}={digest}"


def verify(
    secret: str,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    *,
    now: float | None = None,
    max_skew: int = MAX_SKEW_SECONDS,
) -> None:
    """Raise unless this request is a recent, genuine one from Slack.

    Raises rather than returning a boolean so a caller cannot forget to check
    the result — an `if verify(...)` with no else is a mistake that looks like
    working code.
    """
    if not secret:
        raise SignatureError("no Slack signing secret is configured, so no request can be trusted")
    if not signature or not timestamp:
        raise SignatureError("request is not signed")

    try:
        sent_at = float(timestamp)
    except ValueError as exc:
        raise SignatureError("request timestamp is not a number") from exc

    current = time.time() if now is None else now
    if abs(current - sent_at) > max_skew:
        # Both directions. A future timestamp is as much a sign of tampering as
        # an old one, and accepting it would make the window one-sided.
        raise SignatureError(f"request is {abs(current - sent_at):.0f}s out of date")

    if not hmac.compare_digest(sign(secret, timestamp, body), signature):
        LOG.warning("rejected an unsigned or forged Slack request")
        raise SignatureError("signature does not match")
