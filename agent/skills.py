"""Named, shareable, versioned bundles of prompt, retrieval and action.

PROJECT.md calls this "the useful kernel of Agent Studio without the no-code
builder". The useful kernel is that the thing people actually want to share is
not a model or a workflow — it is *the way we ask this particular question here*:
which words, how much context, whether an action may come out of it. That is
small enough to be a file, which means it can be reviewed, diffed and reverted
like anything else.

A skill is YAML because YAML is what a person edits. Nothing about a skill is
special at runtime: it runs the same agent loop, as the same principal, through
the same permission filter, and produces the same kind of trace. What it changes
is the question, not the machinery.

**A skill is operator configuration, not content.** It arrives from a file an
administrator put in a directory, never from a synced payload, so it lives on
the same side of the trust boundary as the system prompt. That is what allows
it to influence how a question is asked at all.

**A skill cannot widen anything.** Three limits, and each is enforced rather
than documented:

* It runs as the *asking* principal. There is no way to express "run as
  someone else", so no skill can be a route to content its user cannot read.
* Its `actions` list is an intersection with what connectors declare, never a
  union. A skill naming an action nobody can perform is refused at load, not
  discovered at approval time.
* Its inputs are substituted into the *question*, which is the user turn. A
  skill cannot put caller-supplied text into the system channel, because that
  would let whoever runs it write the operator's half of the prompt.

**Loaded with safe_load, always.** `yaml.load` on a file that an administrator
was told is "just config" constructs arbitrary Python objects. The difference
between the two functions is a remote code execution, and the reason to be
explicit here is that the unsafe one is the shorter name.
"""

from __future__ import annotations

import logging
import re
import string
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sync.connectors.registry import action_definitions

LOG = logging.getLogger("hippo.agent.skills")

# A skill name goes into a URL, a job payload and a filename.
VALID_NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# Loud enough to be seen in a diff, cheap enough that nobody skips it.
MAX_QUESTION = 4000
MAX_INPUT = 2000

SUFFIXES = (".yaml", ".yml")


class SkillError(Exception):
    """A skill that will not load, said with enough detail to fix it."""


class SkillInput(BaseModel):
    """One value the caller supplies."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    required: bool = True
    default: str | None = None

    @field_validator("name")
    @classmethod
    def _is_an_identifier(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError(f"{value!r} is not usable as a template placeholder")
        return value


class Skill(BaseModel):
    """One way of asking a question, as a file.

    `question` is a template. Placeholders are `{name}` and are filled from the
    caller's inputs — into the user turn, never into the system prompt.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str = "1"
    description: str = ""
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    inputs: tuple[SkillInput, ...] = ()
    k: int = Field(default=12, ge=1, le=100)
    hops: int = Field(default=1, ge=0, le=2)
    # An intersection with what connectors declare, checked at load. Empty means
    # the skill answers and never proposes, which is the safer default and the
    # one most skills want.
    actions: tuple[str, ...] = ()
    # Where it came from, for the trace and for a person wondering who wrote it.
    source: str = ""

    @field_validator("name")
    @classmethod
    def _is_addressable(cls, value: str) -> str:
        if not VALID_NAME.match(value):
            raise ValueError(
                f"{value!r} is not a usable skill name: lowercase letters, digits and "
                "hyphens, starting with a letter"
            )
        return value

    @model_validator(mode="after")
    def _placeholders_are_declared(self) -> Skill:
        """Every `{name}` in the question has an input, and vice versa.

        A placeholder with no input renders as a literal brace in the question
        and quietly asks something different from what the author meant. An
        input nothing uses is dead configuration that reads as if it does
        something.
        """
        declared = {item.name for item in self.inputs}
        used = _placeholders(self.question)

        missing = sorted(used - declared)
        if missing:
            raise ValueError(f"question uses undeclared input(s): {', '.join(missing)}")

        unused = sorted(declared - used)
        if unused:
            raise ValueError(f"declared input(s) the question never uses: {', '.join(unused)}")
        return self

    @property
    def proposes(self) -> bool:
        return bool(self.actions)

    def render(self, values: dict[str, str] | None = None) -> str:
        """Fill the template. Raises rather than guessing at a missing value.

        Caller-supplied text lands in the question — the user turn — and
        nowhere else. Length is capped per input so a skill cannot be used to
        push a prompt past what a model will read, which would silently drop
        whichever end the provider truncates.
        """
        supplied = dict(values or {})
        filled: dict[str, str] = {}

        for item in self.inputs:
            value = supplied.pop(item.name, None)
            if value is None or value == "":
                if item.required and item.default is None:
                    raise SkillError(f"{self.name}: input {item.name!r} is required")
                value = item.default or ""
            if len(value) > MAX_INPUT:
                raise SkillError(
                    f"{self.name}: input {item.name!r} is {len(value)} characters, "
                    f"the limit is {MAX_INPUT}"
                )
            filled[item.name] = value

        if supplied:
            # Refused rather than ignored: a caller passing `principal_id` and
            # having it silently dropped would reasonably believe it did
            # something.
            raise SkillError(f"{self.name}: unknown input(s): {', '.join(sorted(supplied))}")

        rendered = string.Formatter().vformat(self.question, (), _Filled(filled))
        if len(rendered) > MAX_QUESTION:
            raise SkillError(f"{self.name}: rendered question exceeds {MAX_QUESTION} characters")
        return rendered


class _Filled(dict[str, str]):
    """Formatting map that fails loudly on anything unexpected."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - guarded at validation
        raise SkillError(f"no value for {key!r}")


def _placeholders(template: str) -> set[str]:
    """Field names in a format string, ignoring literal braces."""
    try:
        return {
            name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(template)
            if name
        }
    except ValueError as exc:
        raise ValueError(f"question is not a valid template: {exc}") from exc


def parse(text: str, *, source: str = "") -> Skill:
    """One skill from YAML text.

    `safe_load`, never `load`. The difference between them is arbitrary object
    construction from a file somebody was told is configuration, and the unsafe
    one has the shorter name — which is exactly why this is spelled out rather
    than left to a reader to notice.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SkillError(f"{source or 'skill'}: not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise SkillError(f"{source or 'skill'}: expected a mapping, got {type(raw).__name__}")

    try:
        skill = Skill.model_validate({**raw, "source": source})
    except ValueError as exc:
        raise SkillError(f"{source or 'skill'}: {exc}") from exc

    unknown = sorted(set(skill.actions) - set(action_definitions()))
    if unknown:
        # At load, not at approval. A skill naming an action nobody can perform
        # would otherwise produce a pending row that fails after a person has
        # read it and clicked.
        raise SkillError(
            f"{skill.name}: names action(s) no connector can perform: {', '.join(unknown)}"
        )
    return skill


def load_file(path: Path) -> Skill:
    return parse(path.read_text(encoding="utf-8"), source=str(path))


def load_dir(directory: Path) -> dict[str, Skill]:
    """Every skill in a directory, keyed by name.

    A bad file is refused rather than skipped. Skipping would mean an install
    silently missing a skill somebody thinks is deployed, and the failure would
    surface as "that command does not exist" long after the typo.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}

    skills: dict[str, Skill] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in SUFFIXES:
            continue
        skill = load_file(path)
        if skill.name in skills:
            raise SkillError(
                f"{path}: two skills are named {skill.name!r} "
                f"({skills[skill.name].source} and {path})"
            )
        skills[skill.name] = skill

    LOG.info("skills loaded", extra={"count": len(skills), "directory": str(directory)})
    return skills


class SkillRun(BaseModel):
    """What a skill run produced, alongside the answer."""

    model_config = ConfigDict(frozen=True)

    skill: str
    version: str
    question: str
    inputs: dict[str, str] = Field(default_factory=dict)


def prepare(skill: Skill, values: dict[str, str] | None = None) -> SkillRun:
    """Render a skill into the question it will actually ask.

    Separate from running it so the question can be shown before it is asked —
    a scheduled skill that quietly asks something other than what its author
    wrote is the failure mode this makes visible.
    """
    return SkillRun(
        skill=skill.name,
        version=skill.version,
        question=skill.render(values),
        inputs=dict(values or {}),
    )


def summarise(skills: dict[str, Skill]) -> list[dict[str, Any]]:
    """For the API and the UI. Never includes the raw question template.

    The template is operator configuration and showing it is fine; what is not
    fine is letting it look like something a caller can edit per run, which a
    field next to the inputs would imply.
    """
    return [
        {
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
            "inputs": [item.model_dump() for item in skill.inputs],
            "proposes": skill.proposes,
            "actions": list(skill.actions),
        }
        for skill in sorted(skills.values(), key=lambda item: item.name)
    ]
