"""Risk policy: which actions a human has to click.

ARCHITECTURE §4 point 2 says the policy is a config file, not code, and that
v0's default is that **everything is consequential**. Both halves matter. A
policy in code means changing the blast radius of the agent is a deploy and a
code review by whoever happens to be reviewing; a policy in a file means it is
an operator's decision, visible in one place, diffable.

The default is the interesting part. Deny-by-default for permissions is
uncontroversial; the same instinct applied to actions says an action type
nobody has classified is one nobody has thought about, and the safe reading of
"unthought-about" is "ask a human". So an unknown action type is consequential,
a malformed policy file is a startup failure rather than a silent fallback, and
opting an action into `routine` is something an operator does deliberately.

`routine` now means something. ARCHITECTURE §11 unlocked risk-tiered
auto-approval "after the approval UX and rollback are proven"; the UX landed
with P1-SRF-2 and rollback with P1-SYNC-5, so P2-GOV-1 turns it on — opt-in,
off by default, and hedged about with limits.

The hedging is the point. An operator who writes `auto_approve` is delegating a
decision they will not see, so the failure they are exposed to is not one bad
action but a loop producing many. Every limit below exists to bound that: a
consequential type can never be auto-approved whatever the file says, and both
a global and a per-person hourly cap are on by default rather than opt-in.

TOML rather than YAML because tomllib is in the standard library, and a
dependency added for a twelve-line config file is a dependency to audit
forever.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LOG = logging.getLogger("hippo.agent.policy")

RiskClass = Literal["routine", "consequential"]

CONSEQUENTIAL: RiskClass = "consequential"
ROUTINE: RiskClass = "routine"


class PolicyError(RuntimeError):
    """The policy file is unreadable or says something that is not a policy.

    Never recovered from by falling back to a default. A policy that silently
    became something other than what the operator wrote is worse than one that
    refused to load.
    """


class AutoApproval(BaseModel):
    """When a written policy may stand in for a person's click.

    Every field is a bound rather than a capability. Turning this on is
    delegating a decision nobody will watch, so what matters is not what it
    permits but what it cannot exceed.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    action_types: frozenset[str] = Field(default_factory=frozenset)
    # Caps are on by default and generous rather than absent. The failure an
    # operator is exposed to here is not one wrong action, it is a loop
    # producing hundreds before anyone notices.
    max_per_hour: int = Field(default=20, ge=1)
    max_per_principal_per_hour: int = Field(default=5, ge=1)
    # Empty means anyone whose actions are otherwise eligible. An allow-list of
    # emails is the per-principal boundary: some people's requests may go
    # through unattended, most may not.
    only_principals: frozenset[str] = Field(default_factory=frozenset)

    def covers(self, action_type: str) -> bool:
        return self.enabled and action_type in self.action_types


class RiskPolicy(BaseModel):
    """Which action types are routine, and which may go through unattended."""

    model_config = ConfigDict(frozen=True)

    routine: frozenset[str] = Field(default_factory=frozenset)
    auto_approve: AutoApproval = Field(default_factory=AutoApproval)

    def classify(self, action_type: str) -> RiskClass:
        return ROUTINE if action_type in self.routine else CONSEQUENTIAL

    def may_auto_approve(self, action_type: str, principal_email: str | None = None) -> bool:
        """Whether a policy approval is permitted for this action at all.

        Not whether it *should* happen: the rate limits need database state and
        are applied by the caller. This is the part that can be decided from
        the file alone, and the first check is the one that matters — a
        consequential action is never auto-approvable however the file is
        written.
        """
        if self.classify(action_type) is not ROUTINE:
            return False
        if not self.auto_approve.covers(action_type):
            return False
        allowed = self.auto_approve.only_principals
        return not allowed or (principal_email or "").lower() in allowed

    @property
    def requires_a_human(self) -> bool:
        """Whether every action still waits for a person.

        True unless an operator has opted out, and kept as its own property
        because "does anything happen here without a human" is the question
        someone evaluating this project will ask first.
        """
        return not self.auto_approve.enabled


def load_policy(path: Path | None) -> RiskPolicy:
    """Read the policy file, or return the everything-is-consequential default.

    A missing path is the default and is fine: it is the safest policy there
    is. A path that was given and does not exist is an error, because the
    operator meant to configure something and did not.
    """
    if path is None:
        return RiskPolicy()
    if not path.exists():
        raise PolicyError(f"risk policy file not found: {path}")

    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise PolicyError(f"could not read risk policy {path}: {exc}") from exc

    section = raw.get("actions", {})
    if not isinstance(section, dict):
        raise PolicyError(f"{path}: [actions] must be a table")

    routine: set[str] = set()
    for action_type, value in section.items():
        if value not in (ROUTINE, CONSEQUENTIAL):
            raise PolicyError(
                f"{path}: {action_type} is '{value}', expected 'routine' or 'consequential'"
            )
        if value == ROUTINE:
            routine.add(action_type)

    auto = _auto_approval(path, raw.get("auto_approve", {}), routine)
    policy = RiskPolicy(routine=frozenset(routine), auto_approve=auto)
    LOG.info(
        "risk policy loaded",
        extra={
            "path": str(path),
            "routine": sorted(routine),
            "auto_approve": sorted(auto.action_types) if auto.enabled else [],
        },
    )
    return policy


def _auto_approval(path: Path, section: object, routine: set[str]) -> AutoApproval:
    """Read [auto_approve], refusing anything that would widen a tier.

    Listing a consequential action here is a configuration error rather than a
    way to promote it. An operator who meant to make it routine should say so
    in [actions], where the change is visible next to every other tier — and
    one who did not mean that has just been stopped from delegating something
    they classified as needing a person.
    """
    if not isinstance(section, dict):
        raise PolicyError(f"{path}: [auto_approve] must be a table")

    listed = section.get("action_types", [])
    if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
        raise PolicyError(f"{path}: auto_approve.action_types must be a list of strings")

    promoted = sorted(set(listed) - routine)
    if promoted:
        raise PolicyError(
            f"{path}: {', '.join(promoted)} is not routine, so it cannot be auto-approved. "
            "Classify it in [actions] first if that is what you meant."
        )

    principals = section.get("only_principals", [])
    if not isinstance(principals, list):
        raise PolicyError(f"{path}: auto_approve.only_principals must be a list")

    try:
        return AutoApproval(
            enabled=bool(section.get("enabled", False)),
            action_types=frozenset(listed),
            max_per_hour=int(section.get("max_per_hour", 20)),
            max_per_principal_per_hour=int(section.get("max_per_principal_per_hour", 5)),
            only_principals=frozenset(str(item).lower() for item in principals),
        )
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{path}: {exc}") from exc
