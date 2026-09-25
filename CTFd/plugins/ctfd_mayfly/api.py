"""
Three blueprints:

  - player_bp:   the browser-facing launch/status/stop/extend routes a real player's session
                 hits. Owner is always resolved server-side via util.resolve_owner_id() —
                 NEVER taken from the request body, or a player could act on another team's
                 behalf just by naming a different id in the payload.
  - internal_bp: the orchestrator calling back INTO CTFd to report flag/state/reap events.
                 Authenticated by a shared-secret header, constant-time compared — not a
                 CTFd session, since the caller here is the orchestrator's own backend, not
                 a browser. Production would additionally restrict these by source IP (the
                 orchestrator's known egress ranges), on top of the shared secret.
  - admin_bp:    the admin-only live-instance list + kill button + flag-sharing event log.
"""
import functools
import hmac
import os
from datetime import datetime

from flask import Blueprint, abort, jsonify, render_template, request

from CTFd.models import db
from CTFd.plugins import bypass_csrf_protection
from CTFd.utils.decorators import admins_only, authed_only, ratelimit

from .client import OrchestratorClient, OrchestratorConfigError, OrchestratorRequestError
from .models import MayflyFlagShareEvent, MayflyInstance
from .util import resolve_owner_id

player_bp = Blueprint("ctfd_mayfly_player", __name__, url_prefix="/plugins/ctfd_mayfly")
internal_bp = Blueprint(
    "ctfd_mayfly_internal", __name__, url_prefix="/plugins/ctfd_mayfly/internal"
)
admin_bp = Blueprint(
    "ctfd_mayfly_admin",
    __name__,
    url_prefix="/plugins/ctfd_mayfly/admin",
    template_folder="templates",
)


def _current_live_instance(owner_id: int, challenge_id):
    return (
        MayflyInstance.query.filter_by(owner_id=owner_id, challenge_id=challenge_id)
        .filter(MayflyInstance.state.notin_(("reaped", "failed")))
        .order_by(MayflyInstance.id.desc())
        .first()
    )


def _instance_json(instance: MayflyInstance) -> dict:
    # SQLite/SQLAlchemy's plain db.DateTime strips tzinfo on write, so this comes back
    # naive -- but always UTC in practice, since the orchestrator only ever sends
    # .toISOString() (see internal_update_instance's parsing above). Re-appending "Z" makes
    # that explicit again rather than leaving the frontend's `new Date(...)` to guess: an
    # offset-less ISO string with a time component is ambiguous across JS engines (some
    # treat it as local time, not UTC), which would silently skew the player's countdown by
    # their timezone offset.
    expires_at = instance.expires_at.isoformat() + "Z" if instance.expires_at else None
    return {
        "run_id": instance.run_id,
        "state": instance.state,
        "url": instance.url,
        "expires_at": expires_at,
    }


# --------------------------------------------------------------------------------------
# Player API
# --------------------------------------------------------------------------------------


@player_bp.route("/launch", methods=["POST"])
@authed_only
@ratelimit(method="POST", limit=10, interval=60)
def launch():
    from .challenge import MayflyChallengeModel

    data = request.get_json(silent=True) or {}
    challenge_id = data.get("challenge_id")
    if not challenge_id:
        return jsonify({"error": "challenge_id is required"}), 400

    owner_id = resolve_owner_id()
    if owner_id is None:
        return jsonify({"error": "not authenticated"}), 403

    challenge = MayflyChallengeModel.query.filter_by(id=challenge_id).first()
    if challenge is None:
        return jsonify({"error": "unknown challenge"}), 404

    # Idempotent: a player re-opening the challenge modal, or a flaky double-click, gets the
    # same live instance back rather than a second sandbox for the same owner+challenge.
    existing = _current_live_instance(owner_id, challenge_id)
    if existing is not None:
        return jsonify(_instance_json(existing))

    try:
        launched = OrchestratorClient().launch(
            challenge_id=str(challenge_id),
            team_id=str(owner_id),
            ttl_seconds=challenge.ttl_seconds,
            image=challenge.image,
            port=challenge.port,
            vcpus=challenge.vcpus,
            start_command=challenge.start_command,
        )
    except OrchestratorConfigError as e:
        return jsonify({"error": str(e)}), 500
    except OrchestratorRequestError as e:
        return jsonify({"error": str(e)}), 502

    instance = MayflyInstance(
        owner_id=owner_id,
        challenge_id=challenge_id,
        run_id=launched["run_id"],
        run_token=launched["run_token"],
        state="queued",
    )
    db.session.add(instance)
    db.session.commit()

    # Returns immediately — never blocks on provisioning. CTFd's gunicorn worker pool is
    # exhaustible, and fifty simultaneous launches at an event's opening bell blocking on a
    # sandbox boot would take the platform down. The browser polls status() for the rest.
    return jsonify(_instance_json(instance))


@player_bp.route("/current", methods=["GET"])
@authed_only
def current():
    """Read-only lookup for "is there already a live instance for me on this challenge" --
    unlike launch(), never creates one. Lets the frontend resume showing status/URL when a
    challenge is reopened, instead of the panel defaulting back to the Launch button and the
    player having to click it again just to rediscover an instance that was never actually
    lost (it's sitting fine in this table the whole time -- the browser just never re-asked
    for it, since currentRunId only ever lived in an in-memory JS closure)."""
    raw_challenge_id = request.args.get("challenge_id")
    if not raw_challenge_id:
        return jsonify({"error": "challenge_id is required"}), 400

    # request.args is always a string. On Postgres, filtering an integer column against a
    # non-numeric string (a stale/empty DOM read on the client produces "NaN" or "undefined")
    # raises an unhandled DataError, which Flask turns into an HTML 500 -- exactly the
    # "Unexpected token '<'" JSON-parse error the frontend reports. SQLite never surfaced this
    # since it coerces loosely; validate here so a bad client value fails clean instead.
    try:
        challenge_id = int(raw_challenge_id)
    except ValueError:
        return jsonify({"error": "challenge_id must be an integer"}), 400

    owner_id = resolve_owner_id()
    if owner_id is None:
        return jsonify({"error": "not authenticated"}), 403

    instance = _current_live_instance(owner_id, challenge_id)
    if instance is None:
        return jsonify({"run_id": None})

    return jsonify(_instance_json(instance))


@player_bp.route("/status", methods=["GET"])
@authed_only
def status():
    run_id = request.args.get("run_id")
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400

    owner_id = resolve_owner_id()
    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    # Same 404 whether the run_id doesn't exist or belongs to someone else — don't let a
    # player fingerprint other teams' run ids by distinguishing "not found" from "forbidden".
    if instance is None or instance.owner_id != owner_id:
        return jsonify({"error": "not found"}), 404

    return jsonify(_instance_json(instance))


@player_bp.route("/stop", methods=["POST"])
@authed_only
@ratelimit(method="POST", limit=10, interval=60)
def stop():
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400

    owner_id = resolve_owner_id()
    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    if instance is None or instance.owner_id != owner_id:
        return jsonify({"error": "not found"}), 404

    try:
        OrchestratorClient().stop(run_id, instance.run_token)
    except OrchestratorRequestError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True})


@player_bp.route("/extend", methods=["POST"])
@authed_only
@ratelimit(method="POST", limit=10, interval=60)
def extend():
    data = request.get_json(silent=True) or {}
    run_id = data.get("run_id")
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400

    owner_id = resolve_owner_id()
    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    if instance is None or instance.owner_id != owner_id:
        return jsonify({"error": "not found"}), 404

    try:
        OrchestratorClient().extend(run_id, instance.run_token)
    except OrchestratorRequestError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True})


# --------------------------------------------------------------------------------------
# Internal API — the orchestrator calling back into CTFd
# --------------------------------------------------------------------------------------


def internal_auth_required(f):
    """Shared-secret header, constant-time compared. Not a CSRF-protected CTFd session —
    the caller is the orchestrator's own backend, so every route using this also needs
    @bypass_csrf_protection (CTFd's CSRF nonce is meaningless outside a real browser session
    and would otherwise reject every one of these calls outright)."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-Mayfly-Internal-Secret", "")
        secret = os.environ.get("INSTANCER_SECRET", "")
        if not secret or not hmac.compare_digest(provided, secret):
            abort(403)
        return f(*args, **kwargs)

    return wrapper


@internal_bp.route("/instances/<run_id>/flag", methods=["POST"])
@bypass_csrf_protection
@internal_auth_required
def internal_set_flag(run_id):
    data = request.get_json(silent=True) or {}
    flag_hash = data.get("flag_hash")
    if not flag_hash:
        return jsonify({"error": "flag_hash is required"}), 400

    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    if instance is None:
        return jsonify({"error": "not found"}), 404

    instance.flag_hash = flag_hash
    db.session.commit()
    return jsonify({"ok": True})


@internal_bp.route("/instances/<run_id>", methods=["PATCH"])
@bypass_csrf_protection
@internal_auth_required
def internal_update_instance(run_id):
    data = request.get_json(silent=True) or {}
    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    if instance is None:
        return jsonify({"error": "not found"}), 404

    if "url" in data:
        instance.url = data["url"]
    if "state" in data:
        instance.state = data["state"]
    # Sent on every publishUrl call from the orchestrator -- initial ready AND every
    # successful extend (see lib/ctfd/client.ts) -- so this stays current instead of only
    # ever reflecting the instance's very first healthy publish. None/absent means "unknown",
    # not "no expiry" -- leaves the previous value alone rather than clearing it.
    if "expires_at" in data and data["expires_at"]:
        instance.expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    db.session.commit()
    return jsonify({"ok": True})


@internal_bp.route("/instances/<run_id>/reaped", methods=["POST"])
@bypass_csrf_protection
@internal_auth_required
def internal_mark_reaped(run_id):
    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    if instance is None:
        return jsonify({"error": "not found"}), 404

    instance.state = "reaped"
    instance.url = None
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------------------


@admin_bp.route("/instances", methods=["GET"])
@admins_only
def admin_instances():
    from CTFd.models import Challenges
    from CTFd.utils.modes import get_model

    instances = MayflyInstance.query.order_by(MayflyInstance.created_at.desc()).limit(200).all()
    share_events = (
        MayflyFlagShareEvent.query.order_by(MayflyFlagShareEvent.created_at.desc())
        .limit(50)
        .all()
    )

    # Resolve raw owner_id/challenge_id ints to display names in one batched query each,
    # rather than N+1 lookups per row -- this page can reasonably show ~200 instances.
    account_ids = {i.owner_id for i in instances}
    account_ids.update(e.submitting_owner_id for e in share_events)
    account_ids.update(e.source_owner_id for e in share_events)
    AccountModel = get_model()
    accounts = (
        {a.id: a.name for a in AccountModel.query.filter(AccountModel.id.in_(account_ids)).all()}
        if account_ids
        else {}
    )

    challenge_ids = {i.challenge_id for i in instances}
    challenge_ids.update(e.challenge_id for e in share_events)
    challenges = (
        {c.id: c.name for c in Challenges.query.filter(Challenges.id.in_(challenge_ids)).all()}
        if challenge_ids
        else {}
    )

    return render_template(
        "mayfly_admin_instances.html",
        instances=instances,
        share_events=share_events,
        accounts=accounts,
        challenges=challenges,
    )


@admin_bp.route("/instances/<run_id>/kill", methods=["POST"])
@admins_only
@bypass_csrf_protection
def admin_kill_instance(run_id):
    instance = MayflyInstance.query.filter_by(run_id=run_id).first()
    if instance is None:
        return jsonify({"error": "not found"}), 404

    try:
        OrchestratorClient().stop(run_id, instance.run_token)
    except OrchestratorRequestError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True})
