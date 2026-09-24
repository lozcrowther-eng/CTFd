"""
Talks to the orchestrator (the Vercel control plane in ../ — see its CLAUDE.md for the
invariants this mirrors). Two distinct directions, two distinct auth schemes:

  - Outbound (this plugin -> the orchestrator): launch is HMAC-signed over the raw request
    body, matching the orchestrator's actual enforced contract (readVerifiedBody() in its
    lib/http/signed-request.ts) exactly. status/stop/extend are NOT HMAC-signed -- but they
    are gated by a per-run capability token (run_token) that launch() gets back alongside
    run_id and that every later call must echo on the X-Mayfly-Run-Token header (verified by
    the orchestrator's lib/auth/run-token.ts). This module never computes or checks that
    token's signature itself -- it's minted and verified entirely on the orchestrator side;
    here it's just an opaque string to store on MayflyInstance.run_token and pass back.
  - Inbound (the orchestrator -> this plugin's internal API): a plain shared-secret header,
    constant-time compared -- see api.py's `internal_auth_required`. Simpler than a full
    HMAC signature because the orchestrator side only needs to prove it holds the secret,
    not protect against a re-serialisation mismatch on a body it fully controls itself.

Both directions currently share one secret (INSTANCER_SECRET) for simplicity; nothing stops
them being split into two names later if that's ever warranted.
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

import requests

DEFAULT_TIMEOUT_SECONDS = 5.0
# Bounds the replay window for a signed request, mirroring the orchestrator's own
# DEFAULT_WINDOW_MS in lib/hmac.ts exactly -- a signature is only valid for 5 minutes either
# side of "now", so a captured (body, signature, timestamp) triple stops working after that.
DEFAULT_WINDOW_MS = 5 * 60 * 1000


class OrchestratorConfigError(Exception):
    """Raised when INSTANCER_URL/INSTANCER_SECRET aren't set in the CTFd environment."""


class OrchestratorRequestError(Exception):
    """Raised when the orchestrator responds with a non-2xx status."""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"orchestrator request failed: {status_code} {body}")
        self.status_code = status_code
        self.body = body


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise OrchestratorConfigError(f"{name} must be set in the CTFd environment")
    return value


def sign(raw_body: bytes, secret: str, timestamp_ms: int) -> str:
    """
    HMAC-SHA256 over `{timestamp_ms}.{raw_body}` -- must byte-for-byte match the
    orchestrator's own signer (lib/hmac.ts's `sign()`) so a payload signed here verifies
    there. Takes raw bytes, never a dict, for the same reason the orchestrator insists on
    it: json.dumps and JSON.stringify disagree on whitespace and key order, so anything that
    re-serialises before hashing can silently sign different bytes than what's on the wire.
    Verified against every vector in ../fixtures/hmac-vectors.json (empty body, unicode, a
    100-byte payload, and a plain case) before any other code in this plugin was written.
    """
    message = str(timestamp_ms).encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(
    raw_body: bytes,
    secret: str,
    signature: str,
    timestamp_ms: int,
    window_ms: int = DEFAULT_WINDOW_MS,
    now_ms: Optional[int] = None,
) -> bool:
    """The inverse of sign() -- used nowhere in this plugin yet (nothing calls in expecting
    a full HMAC signature today, only the internal API's shared-secret header), but kept
    alongside sign() since the two are one contract and untested asymmetry invites drift."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if abs(now_ms - timestamp_ms) > window_ms:
        return False
    expected = sign(raw_body, secret, timestamp_ms)
    # Constant-time: a length-dependent early-exit compare would leak how many leading
    # hex characters of a guess were already correct.
    return hmac.compare_digest(expected, signature)


class OrchestratorClient:
    """
    base_url/secret are resolved lazily (on first real use, not at import/construction time)
    so importing this module -- or CTFd's plugin loader constructing one at startup -- never
    requires INSTANCER_URL/INSTANCER_SECRET to already exist. Mirrors the orchestrator's own
    lib/clients.ts, which learned this the hard way: eager module-level env resolution there
    once made `next build` fail on every route whenever the vars were unset.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        secret: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._base_url = base_url
        self._secret = secret
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return (self._base_url or _require_env("INSTANCER_URL")).rstrip("/")

    @property
    def secret(self) -> str:
        return self._secret or _require_env("INSTANCER_SECRET")

    def _signed_post(self, path: str, payload: Dict[str, Any]) -> requests.Response:
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp_ms = int(time.time() * 1000)
        signature = sign(raw_body, self.secret, timestamp_ms)
        return requests.post(
            f"{self.base_url}{path}",
            data=raw_body,
            headers={
                "content-type": "application/json",
                "x-mayfly-timestamp": str(timestamp_ms),
                "x-mayfly-signature": signature,
            },
            timeout=self.timeout,
        )

    def launch(
        self,
        challenge_id: str,
        team_id: str,
        ttl_seconds: int,
        image: Optional[str] = None,
        port: Optional[int] = None,
        vcpus: Optional[int] = None,
        start_command: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        POST /api/instances -> {runId, runToken}. The orchestrator returns this immediately
        without waiting on provisioning (see its CLAUDE.md: a bounded gunicorn worker pool
        here would exhaust itself if fifty simultaneous launches all blocked on a sandbox
        boot) -- so this call is fast by design, not by luck. The browser polls status() for
        the rest. runToken must be stored (MayflyInstance.run_token) and passed to every
        later status()/stop()/extend() call for this run_id -- those routes 401 without it.

        image/port/vcpus/start_command let the orchestrator provision straight from this
        challenge's own MayflyChallengeModel row instead of needing challenge_id to match one
        of its own hardcoded fixtures (lib/fixtures/challenges.ts) -- see its
        lib/api/launch.ts for exactly how it picks between the two.
        """
        payload: Dict[str, Any] = {
            "challengeId": challenge_id,
            "teamId": team_id,
            "ttlSeconds": ttl_seconds,
        }
        # Truthy checks, not `is not None` -- CTFd's admin form submits an empty string for
        # an unfilled optional text field, not null, and the orchestrator's schema requires
        # min-length-1 for these when present (confirmed: sending "" for start_command was
        # rejected with a 400, since an omitted key and a present-but-empty one aren't the
        # same thing to a schema that says .optional() rather than .nullable()).
        if image:
            payload["image"] = image
        if port:
            payload["port"] = port
        if vcpus:
            payload["vcpus"] = vcpus
        if start_command:
            payload["startCommand"] = start_command

        response = self._signed_post("/api/instances", payload)
        if not response.ok:
            raise OrchestratorRequestError(response.status_code, response.text)
        body = response.json()
        return {"run_id": body["runId"], "run_token": body["runToken"]}

    def status(self, run_id: str, run_token: str) -> Dict[str, Any]:
        """GET /api/instances/<run_id> -- gated by the run_token from launch(), not a
        signature; see this module's top-of-file comment."""
        response = requests.get(
            f"{self.base_url}/api/instances/{run_id}",
            headers={"X-Mayfly-Run-Token": run_token},
            timeout=self.timeout,
        )
        if not response.ok:
            raise OrchestratorRequestError(response.status_code, response.text)
        return response.json()

    def stop(self, run_id: str, run_token: str) -> None:
        """POST /api/instances/<run_id>/stop -- same run_token gate as status()."""
        response = requests.post(
            f"{self.base_url}/api/instances/{run_id}/stop",
            headers={"X-Mayfly-Run-Token": run_token},
            timeout=self.timeout,
        )
        if not response.ok:
            raise OrchestratorRequestError(response.status_code, response.text)

    def extend(self, run_id: str, run_token: str) -> None:
        """POST /api/instances/<run_id>/extend -- same run_token gate as status()."""
        response = requests.post(
            f"{self.base_url}/api/instances/{run_id}/extend",
            headers={"X-Mayfly-Run-Token": run_token},
            timeout=self.timeout,
        )
        if not response.ok:
            raise OrchestratorRequestError(response.status_code, response.text)
