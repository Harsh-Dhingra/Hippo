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

from sync.connectors.jira.transport import JiraTransport
from sync.connectors.sdk import (
    DONE,
    AclRecord,
    ContentRecord,
    Cursor,
    IdentityRecord,
    Page,
    SourceRef,
    is_terminal,
)

PROJECT = "jira.project"
ISSUE = "jira.issue"
COMMENT = "jira.comment"

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

    def identities(self, cursor: Cursor) -> Iterator[Page]:
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

    def content(self, cursor: Cursor) -> Iterator[Page]:
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

    def acls(self, cursor: Cursor) -> Iterator[Page]:
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
