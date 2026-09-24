"""
Shared between challenge.py (flag checking) and api.py (the player-facing launch/status/
stop/extend routes) — both need the same "who is making this request" resolution, and it
must never be sourced from the request body: a player could otherwise launch, check status,
or stop an instance on another team's behalf just by naming a different id in the payload.
"""
import hashlib
from typing import Optional

from CTFd.utils import get_config


def resolve_owner_id() -> Optional[int]:
    """Team id in team mode, user id in user mode — CTFd's own account-scoping convention
    (see CTFd.utils.modes.get_model, which the same get_config("user_mode") check drives)."""
    from CTFd.utils.user import get_current_team, get_current_user

    if get_config("user_mode") == "teams":
        team = get_current_team()
        return team.id if team else None

    user = get_current_user()
    return user.id if user else None


def hash_flag(plaintext: str) -> str:
    """sha256 of a submitted flag — CTFd stores only this (MayflyInstance.flag_hash), never
    the plaintext. The orchestrator holds the plaintext, having minted and injected it into
    the sandbox itself; CTFd only ever needs to compare a submission's digest against it."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
