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

What `routine` does *not* do in v0 is execute by itself. ARCHITECTURE §11 lists
risk-tiered auto-execution as unlocked only "after the approval UX and rollback
are proven", which is P1-SYNC-5's job and not yet done. Until then the class is
a label the UI can sort by, and every action waits for a person. Shipping the
label without the auto-execution is deliberate: the config surface is the part
that needs to exist before anyone relies on it, and turning it on later is a
one-line change in a file rather than a schema migration.

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


class RiskPolicy(BaseModel):
    """Which action types are routine. Everything else is consequential."""

    model_config = ConfigDict(frozen=True)

    routine: frozenset[str] = Field(default_factory=frozenset)

    def classify(self, action_type: str) -> RiskClass:
        return ROUTINE if action_type in self.routine else CONSEQUENTIAL

    @property
    def requires_a_human(self) -> bool:
        """True in v0 regardless of classification, and stated as its own
        property so the day that stops being true is one line to find."""
        return True


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

    policy = RiskPolicy(routine=frozenset(routine))
    LOG.info("risk policy loaded", extra={"path": str(path), "routine": sorted(routine)})
    return policy
