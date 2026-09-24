"""
The "mayfly" challenge type — instances are ephemeral Vercel Sandboxes provisioned by the
external orchestrator, with a fresh per-team dynamic flag rather than one static string every
team submits the same answer to. See models.py for why MayflyInstance exists at all: vanilla
CTFd's Flags table is per-challenge, not per-team, so it can't express that.
"""
import hmac
import os

from flask import Blueprint, current_app

from CTFd.models import Challenges, db
from CTFd.plugins.challenges import BaseChallenge

from .client import OrchestratorClient
from .models import MayflyFlagShareEvent, MayflyInstance, TERMINAL_STATES
from .util import hash_flag, resolve_owner_id


class MayflyChallengeModel(Challenges):
    # Explicit, not auto-derived from the class name (Flask-SQLAlchemy's default would be an
    # awkward "mayfly_challenge_model") — this is what migrations/*.py's CREATE TABLE targets.
    __tablename__ = "mayfly_challenges"
    __mapper_args__ = {"polymorphic_identity": "mayfly"}
    id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True
    )
    # A Vercel Container Registry reference — see the orchestrator's lib/sandbox/real-client.ts
    # and lib/fixtures/challenges.ts's `image` field, which this mirrors on the CTFd side.
    image = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=3000)
    ttl_seconds = db.Column(db.Integer, nullable=False, default=1800)
    vcpus = db.Column(db.Integer, nullable=False, default=1)
    # Nullable — falls back to the image's own /start.sh, the orchestrator's convention for a
    # challenge that doesn't need an explicit override (see its CLAUDE.md and real-client.ts's
    # ensureChallengeRunning()).
    start_command = db.Column(db.String(255), nullable=True)


# A challenge's view script is re-fetched by CTFd core on every modal open (it removes and
# re-appends the <script> tag each time -- confirmed in the theme's own bundle), but browsers
# routinely skip re-executing a <script src> that already loaded successfully once on the
# page, regardless of Cache-Control -- caught this after "the Launch button doesn't appear
# until a hard refresh" turned out to be exactly that. A query-string suffix that changes
# whenever the file's contents change forces a genuinely new URL, and therefore a real
# fetch, without needing a manual version bump per release -- the route match is on path
# only, so the query string has no effect on which file Flask actually serves.
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _asset_version(filename: str) -> str:
    try:
        return str(int(os.path.getmtime(os.path.join(_ASSETS_DIR, filename))))
    except OSError:
        return "0"


def _current_live_instance(owner_id: int, challenge_id: int):
    return (
        MayflyInstance.query.filter_by(owner_id=owner_id, challenge_id=challenge_id)
        .filter(MayflyInstance.state.notin_(TERMINAL_STATES))
        .order_by(MayflyInstance.id.desc())
        .first()
    )


class MayflyChallenge(BaseChallenge):
    id = "mayfly"
    name = "mayfly"
    templates = {
        "create": "/plugins/ctfd_mayfly/assets/create.html",
        "update": "/plugins/ctfd_mayfly/assets/update.html",
        "view": "/plugins/ctfd_mayfly/assets/view.html",
    }
    scripts = {
        "create": f"/plugins/ctfd_mayfly/assets/create.js?v={_asset_version('create.js')}",
        "update": f"/plugins/ctfd_mayfly/assets/update.js?v={_asset_version('update.js')}",
        "view": f"/plugins/ctfd_mayfly/assets/view.js?v={_asset_version('view.js')}",
    }
    # Route at which files are accessible — registered via register_plugin_assets_directory()
    # in __init__.py's load(app), matching every bundled CTFd challenge type's own convention.
    route = "/plugins/ctfd_mayfly/assets/"
    blueprint = Blueprint(
        "ctfd_mayfly_challenge", __name__, template_folder="templates", static_folder="assets"
    )
    challenge_model = MayflyChallengeModel

    @classmethod
    def read(cls, challenge):
        challenge = MayflyChallengeModel.query.filter_by(id=challenge.id).first()
        data = super().read(challenge)
        data.update(
            {
                "image": challenge.image,
                "port": challenge.port,
                "ttl_seconds": challenge.ttl_seconds,
                "vcpus": challenge.vcpus,
                "start_command": challenge.start_command,
            }
        )
        return data

    @classmethod
    def attempt(cls, challenge, request):
        """
        Ignores the static Flags table entirely for this challenge type. Vanilla CTFd's flag
        model is per-challenge, not per-team — every team's submission is checked against the
        same shared row(s) — which can't express "team A's correct answer is X, team B's is
        Y." A per-team dynamic flag needs its own owner-scoped lookup instead: MayflyInstance.
        """
        data = request.form or request.get_json()
        submission = (data.get("submission") or "").strip()

        owner_id = resolve_owner_id()
        if owner_id is None:
            return False, "Not authenticated"

        instance = _current_live_instance(owner_id, challenge.id)
        if instance is None:
            return False, "Launch an instance first"

        digest = hash_flag(submission)

        # hmac.compare_digest, not `==` — a submission is attacker-controlled input compared
        # against a secret-derived value, exactly the timing-attack shape this exists for.
        if instance.flag_hash and hmac.compare_digest(digest, instance.flag_hash):
            return True, "Correct"

        shared = (
            MayflyInstance.query.filter(
                MayflyInstance.challenge_id == challenge.id,
                MayflyInstance.owner_id != owner_id,
                MayflyInstance.flag_hash == digest,
            )
            .order_by(MayflyInstance.id.desc())
            .first()
        )
        if shared is not None:
            db.session.add(
                MayflyFlagShareEvent(
                    challenge_id=challenge.id,
                    submitting_owner_id=owner_id,
                    source_owner_id=shared.owner_id,
                )
            )
            db.session.commit()
            return False, "That flag was issued to another team"

        return False, "Incorrect"

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)

        owner_id = resolve_owner_id()
        instance = _current_live_instance(owner_id, challenge.id) if owner_id is not None else None
        if instance is not None:
            try:
                # Fire-and-forget from CTFd's perspective — the orchestrator's own stop
                # endpoint doesn't block on the actual reap (its workflow finishes that
                # asynchronously and calls back into /internal/instances/<run_id>/reaped —
                # see api.py), matching the "launch never blocks" shape the whole orchestrator
                # is built around. A failure here shouldn't fail the solve itself: the
                # instance still reaps on its own TTL even if this call is lost.
                #
                # Deliberately `except Exception`, not just OrchestratorRequestError: the
                # super().solve() call above already committed the Solves row by this point,
                # but CTFd's core /attempt route still runs after this method returns and
                # would turn ANY uncaught exception here into a 500 for the player — turning
                # "the instance-kill side effect timed out" into "your solve looked broken"
                # (confirmed by hitting this directly: an unreachable orchestrator raised
                # OrchestratorConfigError here, uncaught, and crashed the whole request).
                OrchestratorClient().stop(instance.run_id, instance.run_token)
            except Exception:
                current_app.logger.exception(
                    "mayfly: failed to stop orchestrator instance %s after solve (owner=%s, challenge=%s) "
                    "-- solve was still recorded; the instance will reap on its own TTL instead",
                    instance.run_id,
                    owner_id,
                    challenge.id,
                )
