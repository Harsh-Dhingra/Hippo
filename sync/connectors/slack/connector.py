"""Slack, as three read streams.

The permission model is the interesting part, and it is not "who is in the
channel" for every channel. Slack has two different rules:

* A **public** channel is readable by anyone in the workspace, whether or not
  they have joined it. Enumerating its members would under-grant, and would
  also mean re-syncing ACLs every time somebody joins a channel they could
  already read.
* A **private** channel is readable by its members and nobody else. This is the
  case ARCHITECTURE §12 point 2 turns into the demo, so it is the case this
  connector has to get exactly right.

So public channels grant to a synthetic workspace group that every user belongs
to, and private channels grant to their actual members. One rule per Slack
rule, rather than one rule that is wrong half the time.

Messages declare their channel as their container, so a grant on the channel
reaches them without this connector emitting a grant per message.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

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
from sync.connectors.slack.transport import SlackTransport

CHANNEL = "slack.channel"
MESSAGE = "slack.message"

# Every workspace member belongs to this. It is what a public channel grants to.
WORKSPACE_GROUP = "hippo:workspace"


def _next_cursor(body: Mapping[str, Any]) -> str:
    metadata = body.get("response_metadata") or {}
    return str(metadata.get("next_cursor", "") or "")


class SlackConnector:
    """Reads a Slack workspace through a transport."""

    kind = "slack"
    schema_version = "2026-07-01"

    def __init__(self, transport: SlackTransport, *, limit: int = 200) -> None:
        self._transport = transport
        self._limit = limit
        self._channel_cache: list[dict[str, Any]] | None = None

    def _call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._transport.call(method, params)

    # -- identities ---------------------------------------------------------

    def identities(self, cursor: Cursor) -> Iterator[Page]:
        """Users, plus the synthetic workspace group they all belong to.

        The group is emitted on the first page only, so resuming from a later
        cursor still yields exactly the records that follow it.
        """
        if is_terminal(cursor):
            yield Page(records=(), cursor={"page": "", DONE: True}, has_more=False)
            return

        page_cursor = str(cursor.get("page", "") or "")
        first = not cursor

        while True:
            body = self._call("users.list", {"cursor": page_cursor, "limit": self._limit})
            records: list[IdentityRecord] = []
            if first:
                records.append(
                    IdentityRecord(
                        kind="group",
                        source_id=WORKSPACE_GROUP,
                        display_name="Workspace",
                        payload={"synthetic": True, "reason": "public channel visibility"},
                    )
                )
                first = False

            for user in body.get("members", []):
                if user.get("deleted") or user.get("is_bot"):
                    continue
                profile = user.get("profile") or {}
                records.append(
                    IdentityRecord(
                        kind="user",
                        source_id=str(user["id"]),
                        email=profile.get("email"),
                        display_name=profile.get("real_name") or user.get("name"),
                        member_of=(WORKSPACE_GROUP,),
                        payload=dict(user),
                    )
                )

            page_cursor = _next_cursor(body)
            has_more = bool(page_cursor)
            yield Page(
                records=tuple(records),
                cursor={"page": page_cursor} if has_more else {"page": "", DONE: True},
                has_more=has_more,
            )
            if not has_more:
                return

    # -- content ------------------------------------------------------------

    def _channels(self) -> list[dict[str, Any]]:
        if self._channel_cache is None:
            channels: list[dict[str, Any]] = []
            page_cursor = ""
            while True:
                body = self._call(
                    "conversations.list", {"cursor": page_cursor, "limit": self._limit}
                )
                channels.extend(body.get("channels", []))
                page_cursor = _next_cursor(body)
                if not page_cursor:
                    break
            self._channel_cache = channels
        return self._channel_cache

    def _replies(self, channel_id: str, message: Mapping[str, Any]) -> list[ContentRecord]:
        """A thread's replies, emitted alongside the message that started it.

        conversations.history returns thread parents only, so without this the
        substance of most Slack conversations never syncs.
        """
        if not message.get("reply_count"):
            return []
        thread_ts = str(message.get("thread_ts") or message["ts"])
        if thread_ts != str(message["ts"]):
            return []

        records: list[ContentRecord] = []
        page_cursor = ""
        while True:
            body = self._call(
                "conversations.replies",
                {"channel": channel_id, "ts": thread_ts, "cursor": page_cursor},
            )
            for reply in body.get("messages", []):
                if str(reply["ts"]) == thread_ts:
                    continue  # the parent, already emitted
                records.append(self._message_record(channel_id, reply))
            page_cursor = _next_cursor(body)
            if not page_cursor:
                return records

    def _message_record(self, channel_id: str, message: Mapping[str, Any]) -> ContentRecord:
        return ContentRecord(
            source_type=MESSAGE,
            # Slack message ids are only unique within a channel.
            source_id=f"{channel_id}:{message['ts']}",
            payload=dict(message),
            container=SourceRef(source_type=CHANNEL, source_id=channel_id),
        )

    def content(self, cursor: Cursor) -> Iterator[Page]:
        """Channels first, then each channel's messages and threads.

        The cursor names the phase, so a resume knows whether it is still
        listing channels or part way through one channel's history.
        """
        phase = str(cursor.get("phase", "channels"))

        if phase == "done":
            yield Page(records=(), cursor={"phase": "done", DONE: True}, has_more=False)
            return

        if phase == "channels":
            page_cursor = str(cursor.get("page", "") or "")
            while True:
                body = self._call(
                    "conversations.list", {"cursor": page_cursor, "limit": self._limit}
                )
                records = tuple(
                    ContentRecord(
                        source_type=CHANNEL, source_id=str(channel["id"]), payload=dict(channel)
                    )
                    for channel in body.get("channels", [])
                )
                page_cursor = _next_cursor(body)
                nxt: dict[str, Any] = (
                    {"phase": "channels", "page": page_cursor}
                    if page_cursor
                    else {"phase": "messages", "channel": 0, "page": ""}
                )
                yield Page(records=records, cursor=nxt, has_more=True)
                if not page_cursor:
                    break
            index, page_cursor = 0, ""
        else:
            index = int(cursor.get("channel", 0))
            page_cursor = str(cursor.get("page", "") or "")

        channels = self._channels()
        while index < len(channels):
            channel_id = str(channels[index]["id"])
            body = self._call(
                "conversations.history",
                {"channel": channel_id, "cursor": page_cursor, "limit": self._limit},
            )

            records_list: list[ContentRecord] = []
            for message in body.get("messages", []):
                records_list.append(self._message_record(channel_id, message))
                records_list.extend(self._replies(channel_id, message))

            page_cursor = _next_cursor(body)
            if page_cursor:
                nxt = {"phase": "messages", "channel": index, "page": page_cursor}
                has_more = True
            elif index + 1 < len(channels):
                nxt = {"phase": "messages", "channel": index + 1, "page": ""}
                has_more = True
            else:
                nxt = {"phase": "done", DONE: True}
                has_more = False

            yield Page(records=tuple(records_list), cursor=nxt, has_more=has_more)
            if not has_more:
                return
            if not page_cursor:
                index += 1

        yield Page(records=(), cursor={"phase": "done", DONE: True}, has_more=False)

    # -- acls ---------------------------------------------------------------

    def acls(self, cursor: Cursor) -> Iterator[Page]:
        """One page per channel, because that is the unit Slack grants on."""
        channels = self._channels()
        index = int(cursor.get("channel", 0))

        while index < len(channels):
            channel = channels[index]
            channel_id = str(channel["id"])
            target = SourceRef(source_type=CHANNEL, source_id=channel_id)

            if channel.get("is_private"):
                principals = self._members(channel_id)
            else:
                # Readable by the whole workspace, joined or not.
                principals = [WORKSPACE_GROUP]

            records = tuple(
                AclRecord(target=target, principal_source_id=principal) for principal in principals
            )
            index += 1
            has_more = index < len(channels)
            yield Page(
                records=records,
                cursor={"channel": index} if has_more else {"channel": index, DONE: True},
                has_more=has_more,
            )
            if not has_more:
                return

        yield Page(records=(), cursor={"channel": index, DONE: True}, has_more=False)

    def _members(self, channel_id: str) -> list[str]:
        members: list[str] = []
        page_cursor = ""
        while True:
            body = self._call(
                "conversations.members",
                {"channel": channel_id, "cursor": page_cursor, "limit": self._limit},
            )
            members.extend(str(member) for member in body.get("members", []))
            page_cursor = _next_cursor(body)
            if not page_cursor:
                return members
