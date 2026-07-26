"""A real OIDC provider, in-process, on a real socket.

Mocking httpx would not test this. The signing keys are fetched by PyJWT
through urllib rather than through our client, the discovery document is
fetched separately, and the token endpoint is a different host in production —
so a test that stubs one transport proves nothing about the other two. This
serves all three over HTTP on 127.0.0.1 with a genuine RSA key, which means the
signature checks under test are checking a signature.

It is also deliberately faithful about PKCE: the authorize step records the
challenge, and the token endpoint recomputes it from the verifier and refuses a
mismatch. A test that asserts we *send* a challenge proves less than one where
the provider would reject us if we sent the wrong one.

Everything an attacker would tamper with is settable, because those are the
tests worth writing: the algorithm, the issuer, the audience, the nonce, the
expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

DEFAULT_KID = "hippo-test-key"


@dataclass
class Pending:
    """One authorization the provider has issued a code for."""

    nonce: str
    challenge: str | None
    redirect_uri: str


@dataclass
class FakeIdP:
    """An OIDC provider whose every claim the test controls."""

    private_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    kid: str = DEFAULT_KID
    client_id: str = "hippo-test-client"
    client_secret: str = "test-secret"

    # Knobs. Each one is an attack when it disagrees with what we expect.
    algorithm: str = "RS256"
    signing_key: Any = None
    issuer_override: str | None = None
    audience_override: str | None = None
    nonce_override: str | None = None
    expires_in: int = 300
    issued_at_offset: int = 0
    extra_claims: dict[str, Any] = field(default_factory=dict)
    omit_claims: tuple[str, ...] = ()
    email: str | None = "person@example.com"
    email_verified: bool | None = True
    subject: str = "idp-subject-1"
    display_name: str | None = "A Person"
    token_status: int = 200
    omit_id_token: bool = False
    advertise_pkce: bool = True
    auth_methods: tuple[str, ...] = ("client_secret_basic",)
    enforce_pkce: bool = True

    base_url: str = ""
    pending: dict[str, Pending] = field(default_factory=dict)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    # Anything the fixture itself got wrong, so a test failure blames the right
    # thing instead of looking like a dropped connection.
    errors: list[str] = field(default_factory=list)

    # -- what the provider publishes ---------------------------------------

    @property
    def issuer(self) -> str:
        return self.base_url

    def discovery(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "issuer": self.issuer_override if self.issuer_override is not None else self.issuer,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}/token",
            "jwks_uri": f"{self.base_url}/jwks",
            "userinfo_endpoint": f"{self.base_url}/userinfo",
            "token_endpoint_auth_methods_supported": list(self.auth_methods),
        }
        if self.advertise_pkce:
            document["code_challenge_methods_supported"] = ["S256", "plain"]
        return document

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    # -- the handshake -----------------------------------------------------

    def authorize(self, authorization_url: str) -> tuple[str, str]:
        """Stand in for the person clicking through. Returns (code, state)."""
        query = parse_qs(urlparse(authorization_url).query)
        state = query["state"][0]
        code = secrets.token_urlsafe(16)
        self.pending[code] = Pending(
            nonce=query.get("nonce", [""])[0],
            challenge=query.get("code_challenge", [None])[0],
            redirect_uri=query.get("redirect_uri", [""])[0],
        )
        return code, state

    def id_token(self, code: str) -> str:
        pending = self.pending.get(code)
        nonce = self.nonce_override
        if nonce is None:
            nonce = pending.nonce if pending else ""
        now = int(time.time()) + self.issued_at_offset

        claims: dict[str, Any] = {
            "iss": self.issuer_override if self.issuer_override is not None else self.issuer,
            "sub": self.subject,
            "aud": (
                self.audience_override if self.audience_override is not None else self.client_id
            ),
            "exp": now + self.expires_in,
            "iat": now,
            "nonce": nonce,
        }
        if self.email is not None:
            claims["email"] = self.email
        if self.email_verified is not None:
            claims["email_verified"] = self.email_verified
        if self.display_name is not None:
            claims["name"] = self.display_name
        claims.update(self.extra_claims)
        for name in self.omit_claims:
            claims.pop(name, None)

        key = self.signing_key if self.signing_key is not None else self.private_key
        if self.algorithm == "none":
            # PyJWT refuses to *mint* an unsigned token without opting in, which
            # is the right default; assembling it by hand is the only honest way
            # to prove our verifier refuses to accept one.
            header = _b64({"alg": "none", "typ": "JWT", "kid": self.kid})
            return f"{header}.{_b64(claims)}."
        if self.algorithm.startswith("HS"):
            # Same reason, one step further. PyJWT will not use an asymmetric
            # PEM as an HMAC secret at all, so the algorithm-confusion attack
            # cannot be built with the library — which is a good default and a
            # useless fixture. Assembled by hand so the verifier is the thing
            # being tested rather than the signer.
            return _handcrafted_hmac(claims, key, self.algorithm, self.kid)
        return jwt.encode(claims, key, algorithm=self.algorithm, headers={"kid": self.kid})

    def exchange(self, form: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.token_requests.append(form)
        if self.token_status != 200:
            return self.token_status, {"error": "invalid_grant"}

        code = str(form.get("code", ""))
        pending = self.pending.get(code)
        if pending is None:
            return 400, {"error": "invalid_grant"}

        if self.enforce_pkce and pending.challenge is not None:
            verifier = str(form.get("code_verifier", ""))
            if _challenge(verifier) != pending.challenge:
                return 400, {"error": "invalid_grant", "error_description": "PKCE mismatch"}

        body: dict[str, Any] = {"access_token": "at-" + code, "token_type": "Bearer"}
        if not self.omit_id_token:
            body["id_token"] = self.id_token(code)
        return 200, body


def _b64(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _handcrafted_hmac(claims: dict[str, Any], key: Any, algorithm: str, kid: str) -> str:
    """An HS256 token signed with an RSA public key.

    The classic algorithm-confusion forgery: the public key is not a secret, so
    if a verifier reads the algorithm out of the header and hands the key to
    HMAC, anybody holding the JWKS can mint whatever they like.
    """
    digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[algorithm]
    secret = key if isinstance(key, bytes) else str(key).encode()
    signing_input = f"{_b64({'alg': algorithm, 'typ': 'JWT', 'kid': kid})}.{_b64(claims)}"
    signature = hmac.new(secret, signing_input.encode(), digest).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _Handler(BaseHTTPRequestHandler):
    idp: FakeIdP

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet. Test output is for failures."""

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @contextmanager
    def _reporting(self) -> Iterator[None]:
        """Answer with a 500 rather than dropping the socket.

        A handler that raises in its own thread closes the connection and the
        client sees "server disconnected", which reads like a network problem
        and hides the traceback. This makes a broken fixture say what broke.
        """
        try:
            yield
        except Exception as exc:
            self.idp.errors.append(f"{type(exc).__name__}: {exc}")
            with suppress(Exception):
                self._send(500, {"error": "fixture_failed", "detail": str(exc)})

    def do_GET(self) -> None:
        with self._reporting():
            path = urlparse(self.path).path
            if path == "/.well-known/openid-configuration":
                self._send(200, self.idp.discovery())
            elif path == "/jwks":
                self._send(200, self.idp.jwks())
            else:
                self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        with self._reporting():
            self._exchange()

    def _exchange(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = {
            key: values[0] for key, values in parse_qs(self.rfile.read(length).decode()).items()
        }
        if self.headers.get("Authorization", "").startswith("Basic "):
            form["_auth"] = "basic"
        status, body = self.idp.exchange(form)
        self._send(status, body)


class RunningIdP:
    """The provider, listening. Use as a context manager."""

    def __init__(self, idp: FakeIdP) -> None:
        self.idp = idp
        handler = type("Handler", (_Handler,), {"idp": idp})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        idp.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeIdP:
        self._thread.start()
        return self.idp

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
