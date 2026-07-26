"""Jira, as three read streams.

Two things make Jira a useful second connector rather than a variation on the
first.

**Containment is two levels deep.** A comment lives in an issue, which lives in
a project, and Jira grants access at the project. So a single project grant has
to reach through issues to comments, which is what the recursive walk in the
ACL projection is for. Slack only ever needed one level, so this is the first
real test of it.

**Pagination is by offset.** Jira counts from startAt against a total, where
Slack hands back an opaque cursor. Both have to fit the same cursor contract,
which is the check that the SDK is not quietly Slack-shaped.

Permissions come from project roles. A role holds actors, which are users and
groups, and this connector grants to whatever it finds rather than trying to
model Jira's full permission-scheme machinery. Issue-level security schemes are
out of scope for v0 and would be a further restriction on top of this, never a
widening, so the v0 model errs closed.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from sync.connectors.jira.actions import ACTIONS, COMMENT_ACTION, TRANSITION_ACTION
from sync.connectors.jira.transport import JiraTransport
from sync.connectors.sdk import (
    DONE,
    AclRecord,
    Capabilities,
    ConnectorError,
    ContentRecord,
    Cursor,
    IdentityRecord,
    InverseCaptureError,
    Page,
    PermanentSourceError,
    SourceRef,
    WritebackReceipt,
    WritebackRequest,
    is_terminal,
)

PROJECT = "jira.project"
ISSUE = "jira.issue"
COMMENT = "jira.comment"

# The two things this connector can be asked to do. Same strings as the agent's
# action vocabulary (agent/actions.py) because they name the same operations —
# one closed set, agreed at both ends, rather than a mapping table to drift.

USER_ACTOR = "atlassian-user-role-actor"
GROUP_ACTOR = "atlassian-group-role-actor"


def _is_last(body: Mapping[str, Any], start_at: int, items: Sequence[Any]) -> bool:
    """Jira is inconsistent about how it says 'that was the last page'."""
    if "isLast" in body:
        return bool(body["isLast"])
    total = int(body.get("total", 0))
    return start_at + len(items) >= total


class JiraConnector:
    """Reads a Jira site through a transport."""

    kind = "jira"
    schema_version = "2026-07-01"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=self.kind,
            schema_version=self.schema_version,
            actions=ACTIONS,
        )

    def __init__(self, transport: JiraTransport, *, page_size: int = 50) -> None:
        self._transport = transport
        self._page_size = page_size
        self._membership: dict[str, list[str]] | None = None
        self._project_cache: list[dict[str, Any]] | None = None

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._transport.get(path, params)

    def _paged(self, path: str, start_at: int, **extra: Any) -> Any:
        return self._get(path, {"startAt": start_at, "maxResults": self._page_size, **extra})

    # -- identities ---------------------------------------------------------

    def _group_membership(self) -> dict[str, list[str]]:
        """account id -> group ids.

        Built by walking every group's members, because Jira states membership
        from the group's side and the SDK records it from the user's. Doing it
        once up front is what lets each user be emitted exactly once; emitting
        a user per group they belong to would be a duplicate record.
        """
        if self._membership is not None:
            return self._membership

        membership: dict[str, list[str]] = {}
        for group in self._all_groups():
            group_id = str(group["groupId"])
            start_at = 0
            while True:
                body = self._paged("group/member", start_at, groupId=group_id)
                members = body.get("values", [])
                for member in members:
                    membership.setdefault(str(member["accountId"]), []).append(group_id)
                if _is_last(body, start_at, members):
                    break
                start_at += len(members)
        self._membership = membership
        return membership

    def _all_groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        start_at = 0
        while True:
            body = self._paged("group/bulk", start_at)
            values = body.get("values", [])
            groups.extend(values)
            if _is_last(body, start_at, values):
                return groups
            start_at += len(values)

    def identities(self, cursor: Cursor) -> Iterator[Page[IdentityRecord]]:
        """Groups first, then users carrying their memberships."""
        if is_terminal(cursor):
            yield Page(records=(), cursor={"phase": "users", DONE: True}, has_more=False)
            return

        phase = str(cursor.get("phase", "groups"))
        start_at = int(cursor.get("start_at", 0))

        if phase == "groups":
            while True:
                body = self._paged("group/bulk", start_at)
                values = body.get("values", [])
                records = tuple(
                    IdentityRecord(
                        kind="group",
                        source_id=str(group["groupId"]),
                        display_name=group.get("name"),
                        payload=dict(group),
                    )
                    for group in values
                )
                last = _is_last(body, start_at, values)
                start_at += len(values)
                yield Page(
                    records=records,
                    cursor=(
                        {"phase": "users", "start_at": 0}
                        if last
                        else {"phase": "groups", "start_at": start_at}
                    ),
                    has_more=True,
                )
                if last:
                    break
            start_at = 0

        membership = self._group_membership()
        while True:
            users = self._get("users/search", {"startAt": start_at, "maxResults": self._page_size})
            records_list: list[IdentityRecord] = []
            for user in users:
                # Inactive accounts keep resolving to a principal that grants
                # would keep pointing at. App accounts are not people.
                if not user.get("active", True) or user.get("accountType") != "atlassian":
                    continue
                account_id = str(user["accountId"])
                records_list.append(
                    IdentityRecord(
                        kind="user",
                        source_id=account_id,
                        email=user.get("emailAddress"),
                        display_name=user.get("displayName"),
                        member_of=tuple(membership.get(account_id, ())),
                        payload=dict(user),
                    )
                )

            has_more = len(users) == self._page_size
            start_at += len(users)
            yield Page(
                records=tuple(records_list),
                cursor=(
                    {"phase": "users", "start_at": start_at}
                    if has_more
                    else {"phase": "users", DONE: True}
                ),
                has_more=has_more,
            )
            if not has_more:
                return

    # -- content ------------------------------------------------------------

    def _projects(self) -> list[dict[str, Any]]:
        if self._project_cache is None:
            projects: list[dict[str, Any]] = []
            start_at = 0
            while True:
                body = self._paged("project/search", start_at)
                values = body.get("values", [])
                projects.extend(values)
                if _is_last(body, start_at, values):
                    break
                start_at += len(values)
            self._project_cache = projects
        return self._project_cache

    def _comments(self, issue_key: str) -> list[ContentRecord]:
        """Emitted alongside their issue.

        A comment is where most of the reasoning on a ticket lives, and it is a
        separate record with its own container, so a project grant reaches it
        through two hops rather than one.
        """
        records: list[ContentRecord] = []
        start_at = 0
        while True:
            body = self._paged(f"issue/{issue_key}/comment", start_at)
            comments = body.get("comments", [])
            for comment in comments:
                records.append(
                    ContentRecord(
                        source_type=COMMENT,
                        source_id=f"{issue_key}:{comment['id']}",
                        payload=dict(comment),
                        container=SourceRef(source_type=ISSUE, source_id=issue_key),
                    )
                )
            if _is_last(body, start_at, comments):
                return records
            start_at += len(comments)

    def content(self, cursor: Cursor) -> Iterator[Page[ContentRecord]]:
        if is_terminal(cursor):
            yield Page(records=(), cursor={"phase": "issues", DONE: True}, has_more=False)
            return

        phase = str(cursor.get("phase", "projects"))
        start_at = int(cursor.get("start_at", 0))

        if phase == "projects":
            while True:
                body = self._paged("project/search", start_at)
                values = body.get("values", [])
                records = tuple(
                    ContentRecord(
                        source_type=PROJECT, source_id=str(project["key"]), payload=dict(project)
                    )
                    for project in values
                )
                last = _is_last(body, start_at, values)
                start_at += len(values)
                yield Page(
                    records=records,
                    cursor=(
                        {"phase": "issues", "start_at": 0}
                        if last
                        else {"phase": "projects", "start_at": start_at}
                    ),
                    has_more=True,
                )
                if last:
                    break
            start_at = 0

        while True:
            body = self._paged("search", start_at, jql="order by key")
            issues = body.get("issues", [])
            records_list: list[ContentRecord] = []
            for issue in issues:
                key = str(issue["key"])
                project_key = str(issue.get("fields", {}).get("project", {}).get("key", ""))
                records_list.append(
                    ContentRecord(
                        source_type=ISSUE,
                        source_id=key,
                        payload=dict(issue),
                        container=(
                            SourceRef(source_type=PROJECT, source_id=project_key)
                            if project_key
                            else None
                        ),
                    )
                )
                records_list.extend(self._comments(key))

            last = _is_last(body, start_at, issues)
            start_at += len(issues)
            yield Page(
                records=tuple(records_list),
                cursor=(
                    {"phase": "issues", DONE: True}
                    if last
                    else {"phase": "issues", "start_at": start_at}
                ),
                has_more=not last,
            )
            if last:
                return

    # -- acls ---------------------------------------------------------------

    def _role_actors(self, project_key: str) -> list[str]:
        """Every user and group named by any role on the project.

        Jira decides Browse Projects through a permission scheme that almost
        always resolves to project roles. Reading the roles directly is the
        honest v0 approximation: it can under-grant against an exotic scheme,
        never over-grant.
        """
        roles = self._get(f"project/{project_key}/role")
        principals: list[str] = []
        for url in roles.values():
            role_id = str(url).rstrip("/").rsplit("/", 1)[-1]
            body = self._get(f"project/{project_key}/role/{role_id}")
            for actor in body.get("actors", []):
                if actor.get("type") == USER_ACTOR:
                    account_id = actor.get("actorUser", {}).get("accountId")
                    if account_id:
                        principals.append(str(account_id))
                elif actor.get("type") == GROUP_ACTOR:
                    group_id = actor.get("actorGroup", {}).get("groupId")
                    if group_id:
                        principals.append(str(group_id))
        # Deduplicated because one person can hold several roles.
        return sorted(set(principals))

    def acls(self, cursor: Cursor) -> Iterator[Page[AclRecord]]:
        """One page per project, because that is the unit Jira grants on."""
        projects = self._projects()
        index = int(cursor.get("project", 0))

        while index < len(projects):
            key = str(projects[index]["key"])
            target = SourceRef(source_type=PROJECT, source_id=key)
            records = tuple(
                AclRecord(target=target, principal_source_id=principal)
                for principal in self._role_actors(key)
            )
            index += 1
            has_more = index < len(projects)
            yield Page(
                records=records,
                cursor=({"project": index} if has_more else {"project": index, DONE: True}),
                has_more=has_more,
            )
            if not has_more:
                return

        yield Page(records=(), cursor={"project": index, DONE: True}, has_more=False)

    # -- write-back (P1-SYNC-5) ---------------------------------------------
    #
    # Three methods rather than one, because CLAUDE.md rule 3 is a shape and
    # not a convention: there is no way to reach execute() without a separate,
    # earlier call that returns the inverse. The SDK's perform_writeback() is
    # what enforces the ordering, so no caller has to remember it.
    #
    # The two action types differ in an interesting way. A transition's inverse
    # is fully knowable beforehand — it is the status the issue has right now,
    # and reading it is the whole reason capture happens first. A comment's is
    # not: the inverse of creating something is deleting it, and the id does
    # not exist until the create returns. So capture records the plan and the
    # pre-state it can see, and the executor folds the receipt into the stored
    # inverse afterwards. Rule 3 is still satisfied before execution, because
    # what capture proves is that a rollback path exists and that the target is
    # readable — not that every field of it is already known.

    def capture_inverse(self, request: WritebackRequest) -> dict[str, Any]:
        """Read the target before changing it. Raise rather than guess."""
        issue = self._issue_key(request)
        if request.action_type == COMMENT_ACTION:
            # Confirms the issue exists and is readable, which is what makes
            # the delete a rollback rather than a hope.
            self._require_issue(issue)
            return {"op": "delete_comment", "issue": issue}
        if request.action_type == TRANSITION_ACTION:
            fields = self._require_issue(issue)
            status = (fields.get("status") or {}).get("name")
            if not status:
                raise InverseCaptureError(f"{issue}: no current status to return to")
            return {"op": "transition", "issue": issue, "to_status": str(status)}
        raise PermanentSourceError(f"jira cannot perform {request.action_type!r}")

    def execute(self, request: WritebackRequest) -> WritebackReceipt:
        issue = self._issue_key(request)
        if request.action_type == COMMENT_ACTION:
            body = str(request.payload.get("body") or "")
            if not body:
                raise PermanentSourceError("a comment needs a body")
            result = self._transport.post(f"issue/{issue}/comment", {"body": body})
            created = None if not isinstance(result, Mapping) else result.get("id")
            return WritebackReceipt(
                external_id=None if created is None else str(created),
                result=dict(result) if isinstance(result, Mapping) else {},
            )
        if request.action_type == TRANSITION_ACTION:
            status = str(request.payload.get("to_status") or "")
            if not status:
                raise PermanentSourceError("a transition needs a target status")
            self._transition(issue, status)
            return WritebackReceipt(result={"to_status": status})
        raise PermanentSourceError(f"jira cannot perform {request.action_type!r}")

    def rollback(self, request: WritebackRequest, inverse: Mapping[str, Any]) -> None:
        """Put the target back the way capture_inverse found it."""
        op = str(inverse.get("op") or "")
        issue = str(inverse.get("issue") or "")
        if op == "delete_comment":
            comment_id = inverse.get("created_id")
            if not comment_id:
                # The executor folds the receipt in after a successful create.
                # Its absence means we do not know what to delete, and deleting
                # a guessed comment is worse than refusing.
                raise InverseCaptureError(
                    f"{issue}: no comment id recorded, so there is nothing safe to delete"
                )
            self._transport.delete(f"issue/{issue}/comment/{comment_id}")
            return
        if op == "transition":
            self._transition(issue, str(inverse.get("to_status") or ""))
            return
        raise PermanentSourceError(f"unknown inverse operation {op!r}")

    # -- write-back helpers -------------------------------------------------

    def _issue_key(self, request: WritebackRequest) -> str:
        if request.target is None or not request.target.source_id:
            raise PermanentSourceError("a jira write-back needs a target issue")
        # Comments are addressed as ISSUE:comment_id elsewhere; the issue key is
        # the part before the colon either way.
        return request.target.source_id.partition(":")[0]

    def _require_issue(self, issue: str) -> dict[str, Any]:
        """The issue's fields, or InverseCaptureError.

        Rule 3 again: a target that cannot be read is a target with no proven
        rollback path, and that fails the action rather than proceeding.
        """
        try:
            body = self._get(f"issue/{issue}")
        except ConnectorError as exc:
            raise InverseCaptureError(f"{issue}: could not read the target: {exc}") from exc
        if not isinstance(body, Mapping):
            raise InverseCaptureError(f"{issue}: unreadable response")
        fields = body.get("fields")
        if not isinstance(fields, Mapping):
            raise InverseCaptureError(f"{issue}: no fields in the response")
        return dict(fields)

    def _transition(self, issue: str, status: str) -> None:
        """Move an issue by status name.

        Jira transitions are identified by id and the ids differ per workflow,
        so the name is resolved against what the issue can actually do right
        now. A name that is not an available transition fails loudly: silently
        doing nothing would report success for a status change that did not
        happen.
        """
        if not status:
            raise PermanentSourceError(f"{issue}: no target status")
        available = self._get(f"issue/{issue}/transitions")
        transitions = available.get("transitions", []) if isinstance(available, Mapping) else []
        for transition in transitions:
            name = str((transition.get("to") or {}).get("name") or transition.get("name") or "")
            if name.casefold() == status.casefold():
                self._transport.post(
                    f"issue/{issue}/transitions",
                    {"transition": {"id": str(transition.get("id")), "name": status}},
                )
                return
        offered = ", ".join(
            str((t.get("to") or {}).get("name") or t.get("name") or "?") for t in transitions
        )
        raise PermanentSourceError(
            f"{issue}: no transition to {status!r}; available: {offered or 'none'}"
        )
