"""The GitHub connector."""

from sync.connectors.github.connector import GitHubConnector
from sync.connectors.github.transport import FixtureTransport, GitHubTransport, HttpTransport

__all__ = ["FixtureTransport", "GitHubConnector", "GitHubTransport", "HttpTransport"]
