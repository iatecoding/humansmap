"""Shared contract between feed generation and feed serving."""

# Increment when generated card semantics change and existing Redis content
# must be rebuilt. This is independent from the Redis key-layout version.
FEED_CONTENT_VERSION = 1
