"""
MayflyInstance tracks one launched sandbox per (owner, challenge) — the CTFd-side record of
what the orchestrator's instance-lifecycle.ts calls "one sandbox per team per challenge"
(see its CLAUDE.md). This table is what lets attempt() (challenge.py) find "this owner's
current flag" without a static Flags row, and what the admin instance list reads from.

No unique constraint on (owner_id, challenge_id): an owner can legitimately have several
historical rows for the same challenge over time (launch, solve/reap, launch again later) —
only the *current live* one matters for flag-checking and launch idempotency, and that's a
query concern (state not in ("reaped", "failed")), not a schema one.
"""
import datetime

from CTFd.models import db

LIVE_STATES = ("queued", "provisioning", "healthy", "expiring")
TERMINAL_STATES = ("reaped", "failed")


class MayflyInstance(db.Model):
    __tablename__ = "mayfly_instances"

    id = db.Column(db.Integer, primary_key=True)
    # The team id in team mode, the user id in user mode — resolved once at launch time via
    # util.resolve_owner_id(), never re-derived from a request body (see api.py's comment on
    # why: a player could otherwise launch on another team's behalf).
    owner_id = db.Column(db.Integer, nullable=False)
    challenge_id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False
    )
    # The orchestrator's own workflow run id — the one identifier both sides agree on.
    run_id = db.Column(db.String(64), nullable=False, unique=True)
    # Opaque capability token the orchestrator mints at launch (see its lib/auth/run-token.ts)
    # and hands back alongside run_id. This plugin never computes or verifies it -- only
    # stores and echoes it back on status/stop/extend, which is what lets those routes be
    # addressed by something stronger than an unguessable run_id alone.
    run_token = db.Column(db.String(128), nullable=True)
    url = db.Column(db.String(512), nullable=True)
    # queued|provisioning|healthy|expiring|reaped|failed — mirrors the orchestrator's own
    # InstanceState (lib/types.ts) so the two sides never invent divergent vocabularies.
    state = db.Column(db.String(32), nullable=False, default="queued")
    # sha256(plaintext flag), never the plaintext itself — the orchestrator holds the
    # plaintext (it minted it and injected it into the sandbox); CTFd only ever needs to
    # compare a submission's digest against this (see challenge.py's attempt()).
    flag_hash = db.Column(db.String(64), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)

    __table_args__ = (
        db.Index("ix_mayfly_instances_owner_challenge", "owner_id", "challenge_id"),
    )

    @property
    def is_live(self) -> bool:
        return self.state not in TERMINAL_STATES

    def __repr__(self):
        return f"<MayflyInstance {self.run_id} owner={self.owner_id} state={self.state}>"


class MayflyFlagShareEvent(db.Model):
    """
    Logged whenever a submission's digest matches ANOTHER owner's instance — see
    challenge.py's attempt(). Purely a paper trail for admins to notice sharing/leaking;
    never used to gate anything automatically (the submission itself is still just marked
    incorrect, same as any other wrong answer).
    """

    __tablename__ = "mayfly_flag_share_events"

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False
    )
    submitting_owner_id = db.Column(db.Integer, nullable=False)
    source_owner_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)

    def __repr__(self):
        return (
            f"<MayflyFlagShareEvent challenge={self.challenge_id} "
            f"submitting_owner={self.submitting_owner_id} source_owner={self.source_owner_id}>"
        )
