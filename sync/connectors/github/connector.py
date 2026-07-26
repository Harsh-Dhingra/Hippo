"""Reading a GitHub organisation.

The third connector, written to find out whether SDK v1 generalises rather than
because GitHub was next on a list. Three things it does that neither Slack nor
Jira does, each of which is a place the contract could have been too narrow:

**The cursor is not in the payload.** GitHub pages with a `Link` header, so the
resume token is a URL the server built. It goes into the cursor verbatim — which
works only because the runtime treats a cursor as opaque jsonb and never
inspects it. If the SDK had specified a shape, this connector would need a
translation layer that guessed at page boundaries.

**Visibility is not per object.** A repository is private or public, and every
issue in it inherits that. So the ACL stream grants on repositories, and each
issue declares its repository as its container — the same containment idea as a
Slack channel, arriving through a different door.

**Public repositories have no ACL rows at all.** A public repo is readable by
everyone in the organisation, and the deny-by-default filter has no concept of
"everyone". Emitting a grant per member per public repo would be quadratic and
wrong the moment somebody joins. Instead the org membership group is granted,
which is one row and stays correct.

**Issues and pull requests are the same object.** GitHub's issues endpoint
returns both, distinguished by a `pull_request` key. They are kept as one entity
type rather than split, because "the PR that closed this" and "the issue it
closed" are the same conversation and separating them would break the timeline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from sync.connectors.github.actions import (
    ACTIONS,
    CLOSE_ACTION,
    COMMENT_ACTION,
    ClosePayload,
)
from sync.connectors.github.transport import GitHubTransport
from sync.connectors.sdk import (
    DONE,
    AclRecord,
    Capabilities,
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

LOG = logging.getLogger("hippo.sync.connectors.github")

REPO = "github.repo"
ISSUE = "github.issue"
COMMENT = "github.comment"

# Every member of the organisation belongs to this. It is what a public
# repository grants to — one row, rather than one per member per repo.
ORG_GROUP = "hippo:org-members"

# Cursor keys. Named so a stored cursor is readable in the database when
# somebody is working out why a sync is behaving oddly.
NEXT = "next"
REPOS = "repos"
INDEX = "index"


class GitHubConnector:
    """Reads one GitHub organisation through a transport."""

    kind = "github"
    schema_version = "2026-07-01"

    def __init__(self, transport: GitHubTransport, org: str) -> None:
        if not org:
            raise PermanentSourceError("github connector needs an organisation")
        self._transport = transport
        self._org = org
        self._repo_cache: list[dict[str, Any]] | None = None

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kind=self.kind,
            schema_version=self.schema_version,
            actions=ACTIONS,
        )

    # -- paging -------------------------------------------------------------

    def _fetch(self, path: str) -> tuple[list[dict[str, Any]], str | None]:
        response = self._transport.get(path)
        body = response.body
        items = [dict(item) for item in body] if isinstance(body, list) else []
        return items, response.next_url

    def _all(self, path: str) -> list[dict[str, Any]]:
        """Walk every page. For the small lists that back other streams."""
        items, next_url = self._fetch(path)
        while next_url:
            more, next_url = self._fetch(next_url)
            items.extend(more)
        return items

    def _repos(self) -> list[dict[str, Any]]:
        if self._repo_cache is None:
            self._repo_cache = self._all(f"/orgs/{self._org}/repos")
        return self._repo_cache

    # -- identities ---------------------------------------------------------

    def identities(self, cursor: Cursor) -> Iterator[Page[IdentityRecord]]:
        """Members and teams.

        A listing stream: GitHub has no incremental members endpoint, so its
        terminal cursor means finished rather than a position, and the runtime
        starts the next pass from empty.

        The organisation group is emitted first and always. Public repositories
        grant to it, so a sync that produced repositories before the group they
        grant to would leave those grants unprojectable until the next pass.
        """
        if is_terminal(cursor):
            yield Page(records=(), cursor=dict(cursor))
            return

        records: list[IdentityRecord] = [
            IdentityRecord(
                kind="group",
                source_id=ORG_GROUP,
                display_name=f"{self._org} members",
                payload={"org": self._org, "synthetic": True},
            )
        ]

        teams = self._all(f"/orgs/{self._org}/teams")
        for team in teams:
            records.append(
                IdentityRecord(
                    kind="group",
                    source_id=f"team:{team['slug']}",
                    display_name=str(team.get("name") or team["slug"]),
                    payload=team,
                )
            )

        # Team membership per member, so a person carries every group they are
        # in. GitHub only offers it the other way round, hence the inversion.
        memberships: dict[str, list[str]] = {}
        for team in teams:
            for member in self._all(f"/orgs/{self._org}/teams/{team['slug']}/members"):
                memberships.setdefault(str(member["login"]), []).append(f"team:{team['slug']}")

        for member in self._all(f"/orgs/{self._org}/members"):
            login = str(member["login"])
            records.append(
                IdentityRecord(
                    kind="user",
                    source_id=login,
                    # Often absent: GitHub hides it unless the account chose to
                    # publish it. Without one, identity resolution cannot link
                    # this person to their Slack or Jira account, and the
                    # honest outcome is that it does not try.
                    email=member.get("email"),
                    display_name=member.get("name") or login,
                    member_of=(ORG_GROUP, *memberships.get(login, ())),
                    payload=member,
                )
            )

        yield Page(records=tuple(records), cursor={DONE: True})

    # -- content ------------------------------------------------------------

    def content(self, cursor: Cursor) -> Iterator[Page[ContentRecord]]:
        """Issues, pull requests and their comments, repository by repository.

        The cursor carries the repository list it started with, not just a
        position in it. A repository created mid-sync would otherwise shift
        every later index and silently skip whatever moved past the cursor.
        """
        if is_terminal(cursor):
            yield Page(records=(), cursor=dict(cursor))
            return

        repos: list[str] = list(cursor.get(REPOS) or [])
        if not repos:
            repos = [str(repo["full_name"]) for repo in self._repos()]
        index = int(cursor.get(INDEX, 0))
        next_url = cursor.get(NEXT)

        if index >= len(repos):
            yield Page(records=(), cursor={DONE: True})
            return

        # A generator, not one page per call. The runtime pulls until
        # has_more is False, so returning after a single page would leave the
        # last page it saw still claiming there is more — and every repository
        # after the first would never be read.
        while index < len(repos):
            full_name = repos[index]
            container = SourceRef(source_type=REPO, source_id=full_name)
            path = str(next_url or f"/repos/{full_name}/issues?state=all")
            issues, following = self._fetch(path)

            records: list[ContentRecord] = []
            for issue in issues:
                number = int(issue["number"])
                records.append(
                    ContentRecord(
                        source_type=ISSUE,
                        source_id=f"{full_name}#{number}",
                        payload=issue,
                        container=container,
                    )
                )
                if int(issue.get("comments", 0)):
                    records.extend(self._comments(full_name, number, container))

            if following:
                next_cursor: dict[str, Any] = {REPOS: repos, INDEX: index, NEXT: following}
            elif index + 1 < len(repos):
                next_cursor = {REPOS: repos, INDEX: index + 1}
            else:
                next_cursor = {DONE: True}

            done = bool(next_cursor.get(DONE, False))
            yield Page(records=tuple(records), cursor=next_cursor, has_more=not done)
            if done:
                return

            index = int(next_cursor.get(INDEX, index))
            next_url = next_cursor.get(NEXT)

    def _comments(self, full_name: str, number: int, container: SourceRef) -> list[ContentRecord]:
        """A comment's container is its repository, not its issue.

        Access is granted on the repository, and the permission filter reads
        containers. Pointing a comment at its issue would be truer to the
        conversation and would leave the comment ungranted.
        """
        return [
            ContentRecord(
                source_type=COMMENT,
                source_id=f"{full_name}#{number}:{comment['id']}",
                payload=comment,
                container=container,
            )
            for comment in self._all(f"/repos/{full_name}/issues/{number}/comments")
        ]

    # -- acls ---------------------------------------------------------------

    def acls(self, cursor: Cursor) -> Iterator[Page[AclRecord]]:
        """Who can read which repository.

        Public repositories grant to the organisation group rather than to each
        member: one row that stays correct when somebody joins, instead of a
        row per member per repository that is wrong the moment they do.

        Private repositories grant to their collaborators and to the teams with
        access. A team is a principal, so its members inherit through the same
        group expansion the permission filter already does.
        """
        if is_terminal(cursor):
            yield Page(records=(), cursor=dict(cursor))
            return

        records: list[AclRecord] = []
        for repo in self._repos():
            full_name = str(repo["full_name"])
            target = SourceRef(source_type=REPO, source_id=full_name)

            if not repo.get("private", True):
                records.append(AclRecord(target=target, principal_source_id=ORG_GROUP))
                continue

            for collaborator in self._all(f"/repos/{full_name}/collaborators"):
                records.append(
                    AclRecord(target=target, principal_source_id=str(collaborator["login"]))
                )
            for team in self._all(f"/repos/{full_name}/teams"):
                records.append(AclRecord(target=target, principal_source_id=f"team:{team['slug']}"))

        yield Page(records=tuple(records), cursor={DONE: True})

    # -- writeback ----------------------------------------------------------

    def _issue_path(self, request: WritebackRequest) -> tuple[str, int]:
        if request.target is None or request.target.source_type != ISSUE:
            raise InverseCaptureError("github write-back needs an issue target")
        full_name, _, number = str(request.target.source_id).partition("#")
        if not full_name or not number.isdigit():
            raise InverseCaptureError(f"unrecognised issue id {request.target.source_id!r}")
        return full_name, int(number)

    def capture_inverse(self, request: WritebackRequest) -> dict[str, Any]:
        """The state to restore, which differs by action.

        Commenting is undone by deleting what was created, so the inverse is
        only a marker and the id arrives in the receipt. Closing is undone by
        restoring the state that was there before, which has to be read now —
        an issue closed and then reopened by somebody else must not be closed
        again by a rollback.
        """
        full_name, number = self._issue_path(request)
        try:
            issue = self._transport.get(f"/repos/{full_name}/issues/{number}").body
        except PermanentSourceError as exc:
            raise InverseCaptureError(f"cannot read {full_name}#{number}: {exc}") from exc
        if not isinstance(issue, Mapping):
            raise InverseCaptureError(f"unexpected response for {full_name}#{number}")

        if request.action_type == COMMENT_ACTION:
            return {"kind": "comment", "repo": full_name, "issue": number}
        if request.action_type == CLOSE_ACTION:
            return {
                "kind": "state",
                "repo": full_name,
                "issue": number,
                "state": str(issue.get("state", "open")),
                "state_reason": issue.get("state_reason"),
            }
        raise InverseCaptureError(f"github cannot perform {request.action_type!r}")

    def execute(self, request: WritebackRequest) -> WritebackReceipt:
        full_name, number = self._issue_path(request)

        if request.action_type == COMMENT_ACTION:
            created = self._transport.post(
                f"/repos/{full_name}/issues/{number}/comments",
                {"body": str(request.payload.get("body", ""))},
            )
            identifier = created.get("id") if isinstance(created, Mapping) else None
            return WritebackReceipt(
                external_id=None if identifier is None else str(identifier),
                result=dict(created) if isinstance(created, Mapping) else {},
            )

        if request.action_type == CLOSE_ACTION:
            reason = ClosePayload.model_validate(dict(request.payload)).reason
            updated = self._transport.patch(
                f"/repos/{full_name}/issues/{number}",
                {"state": "closed", "state_reason": reason},
            )
            return WritebackReceipt(
                external_id=f"{full_name}#{number}",
                result=dict(updated) if isinstance(updated, Mapping) else {},
            )

        raise PermanentSourceError(f"github cannot perform {request.action_type!r}")

    def rollback(self, request: WritebackRequest, inverse: Mapping[str, Any]) -> None:
        """Undo, from what was captured rather than from what was returned."""
        kind = str(inverse.get("kind", ""))
        full_name = str(inverse.get("repo", ""))
        number = inverse.get("issue")

        if kind == "comment":
            # The executor folds the receipt's id into the stored inverse after
            # a successful create — nothing captured beforehand could know it.
            # Its absence means we do not know what to delete, and deleting a
            # guessed comment is worse than refusing.
            comment_id = inverse.get("created_id")
            if not comment_id:
                raise InverseCaptureError(
                    f"{full_name}#{number}: no comment id recorded, so there is "
                    "nothing safe to delete"
                )
            self._transport.delete(f"/repos/{full_name}/issues/comments/{comment_id}")
            return

        if kind == "state":
            self._transport.patch(
                f"/repos/{full_name}/issues/{number}",
                {
                    "state": str(inverse.get("state", "open")),
                    "state_reason": inverse.get("state_reason"),
                },
            )
            return

        raise PermanentSourceError(f"github cannot roll back {kind!r}")


def issue_numbers(records: Sequence[ContentRecord]) -> tuple[str, ...]:
    """Issue ids out of a page. Used by the tests and by nothing else."""
    return tuple(record.source_id for record in records if record.source_type == ISSUE)
