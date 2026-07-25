"""The demo world: a fixture Slack workspace and Jira site, seeded into a
database so the product can be tried before anyone connects a real one.

Shipped in the image on purpose. "docker compose up, sign in, see the filtered
path" is the adoption path for a self-hosted tool, and requiring a Slack token
before anything can be seen would put a procurement conversation in front of
the first thirty seconds. The corpus is a few kilobytes of JSON and the module
does nothing unless it is run.
"""
