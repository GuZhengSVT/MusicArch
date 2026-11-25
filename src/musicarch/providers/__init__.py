"""Read-only music providers."""

from dataclasses import dataclass, field

from ..store import MusicArchError


class ProviderError(MusicArchError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def incomplete(message):
    return ProviderError("incomplete", message)


@dataclass(frozen=True)
class Track:
    id: str
    url: str | None = None
    status: str = "unresolved"
    metadata: dict = field(default_factory=dict)
    field_sources: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Collection:
    key: str
    type: str
    name: str
    visibility: str = "unknown"
    playlist_id: str | None = None


@dataclass(frozen=True)
class Member:
    track: Track
    added_at: str | None = None


@dataclass(frozen=True)
class Page:
    items: list
    total: int | None = None
    next_offset: int | None = None
