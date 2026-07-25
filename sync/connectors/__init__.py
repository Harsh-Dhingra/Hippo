"""The connector SDK.

The most public API in the repo. Phase 3 succeeds or fails on whether people
who have never read the rest of this codebase can implement it in a weekend,
so the contract is deliberately small and everything it can enforce, it does.
"""

from sync.connectors.sdk import (
    AclRecord,
    ConnectorError,
    ContentRecord,
    Cursor,
    IdentityRecord,
    InverseCaptureError,
    Page,
    PermanentSourceError,
    RateLimitedError,
    ReadConnector,
    SourceRef,
    TransientSourceError,
    WritebackConnector,
    WritebackReceipt,
    WritebackRequest,
    perform_writeback,
    unknown_fields,
)

__all__ = [
    "AclRecord",
    "ConnectorError",
    "ContentRecord",
    "Cursor",
    "IdentityRecord",
    "InverseCaptureError",
    "Page",
    "PermanentSourceError",
    "RateLimitedError",
    "ReadConnector",
    "SourceRef",
    "TransientSourceError",
    "WritebackConnector",
    "WritebackReceipt",
    "WritebackRequest",
    "perform_writeback",
    "unknown_fields",
]
