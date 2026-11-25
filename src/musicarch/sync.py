"""Collect complete snapshots before replacing collection memberships."""

import json
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from itertools import islice

from .catalog import save_track
from .providers import Collection, ProviderError, incomplete
from .store import bind_account, check_account, collection_selector, encoded, transaction


@dataclass
class SyncReport:
    collections: int = 0
    memberships: int = 0
    sources: set = field(default_factory=set)
    unresolved: set = field(default_factory=set)
    failures: list = field(default_factory=list)
    enriched: int = 0

    def fail(self, key, error):
        self.failures.append((key, error.kind, str(error)))


def read_pages(fetch):
    offset = seen = 0
    total = None
    while True:
        page = fetch(offset)
        if page.total is not None:
            if total is not None and total != page.total:
                raise incomplete("分页的预期总数发生变化。")
            total = page.total
        seen += len(page.items)
        if (total is not None and seen > total
                or page.next_offset is None and total is not None and seen != total):
            raise incomplete(f"成员或目录条数不符，收到 {seen} 条，预计 {total} 条。")
        if page.next_offset is not None and (not page.items or page.next_offset != seen):
            raise incomplete("分页偏移重复或跳过条目。")
        yield from page.items
        if page.next_offset is None:
            break
        offset = page.next_offset


def complete_tracks(provider, members, db, album_cache):
    tracks = {}
    for member in members:
        track = member.track
        if track.id not in tracks or len(track.metadata) > len(tracks[track.id].metadata):
            tracks[track.id] = track
    ids = iter(tracks)
    while batch := list(islice(ids, provider.batch_size)):
        details = provider.fetch_tracks(batch)
        for track_id, detail in details.items():
            slim = tracks[track_id]
            tracks[track_id] = replace(detail, metadata=slim.metadata | detail.metadata,
                                       field_sources=slim.field_sources | detail.field_sources)
    return enrich_albums(provider, db, tracks, album_cache)


def enrich_albums(provider, db, tracks, cache):
    for track_id, track in tracks.items():
        album_id = track.metadata.get("album_id")
        if not album_id or all(track.metadata.get(key) for key in ("release_date", "artwork_url")):
            continue
        previous = db.execute("SELECT metadata FROM sources WHERE platform = ? AND track_id = ?",
                              (provider.platform, track_id)).fetchone()
        old = json.loads(previous["metadata"]) if previous else {}
        if old.get("album_id") == album_id and all(old.get(key) or track.metadata.get(key)
                                                  for key in ("release_date", "artwork_url")):
            continue
        if album_id not in cache:
            try:
                cache[album_id] = provider.fetch_album(album_id)
            except ProviderError as error:
                if error.kind == "auth":
                    raise
                # Album details are optional. A failed lookup carries no availability signal.
                cache[album_id] = {}
        album = {key: value for key, value in cache[album_id].items() if key not in track.metadata}
        tracks[track_id] = replace(track, metadata=track.metadata | album,
                                   field_sources=track.field_sources | dict.fromkeys(album, "album_detail"))
    return tracks


def assert_identity(provider, account_id):
    if provider.account_id() != account_id:
        raise ProviderError("auth", "同步中登录账号发生变化，已保留上次快照。")


def enrich_track_tags(db, provider, track_ids):
    now = int(time.time())
    eligible = db.execute("""
        SELECT track_id, source.metadata FROM sources AS source
        LEFT JOIN track_tag_checks USING (platform, track_id)
        WHERE source.platform = ? AND source.url IS NOT NULL AND source.status != 'unresolved'
          AND (checked_at IS NULL OR checked_at < ?)
        ORDER BY track_id
    """, (provider.platform, now - 30 * 86400))
    pending = []
    for row in eligible:
        if row["track_id"] in track_ids:
            metadata = json.loads(row["metadata"])
            if not metadata.get("language") or not metadata.get("genre"):
                pending.append(row["track_id"])

    def fetch(track_id):
        try:
            return provider.fetch_track_tags(track_id)
        except ProviderError as error:
            return error

    successes = failures = 0
    pending = iter(pending)
    with ThreadPoolExecutor(max_workers=4) as workers:
        while batch := list(islice(pending, 32)):
            results = list(workers.map(fetch, batch))
            with transaction(db):
                for track_id, result in zip(batch, results):
                    if isinstance(result, ProviderError):
                        failures += 1
                        continue
                    key = provider.platform, track_id
                    row = db.execute("SELECT metadata, field_sources FROM sources WHERE (platform, track_id) = (?, ?)", key).fetchone()
                    fields = json.loads(row["metadata"])
                    added = {field: value for field, value in result.items() if not fields.get(field)}
                    if added:
                        origins = json.loads(row["field_sources"]) | dict.fromkeys(added, "platform_detail")
                        db.execute("UPDATE sources SET metadata = ?, field_sources = ? WHERE (platform, track_id) = (?, ?)",
                                   (encoded(fields | added), encoded(origins), *key))
                        successes += 1
                    db.execute("""INSERT INTO track_tag_checks VALUES (?, ?, ?)
                                  ON CONFLICT (platform, track_id) DO UPDATE SET checked_at = excluded.checked_at""", (*key, now))
            if any(isinstance(result, ProviderError) and result.kind in {"auth", "rate_limit"} for result in results):
                failures += sum(1 for _ in pending)
                break
    return successes, failures


def save_collection(db, platform, account_id, collection, members, tracks):
    scope = (platform, account_id, collection.key)
    with transaction(db):
        db.execute("""
            INSERT INTO collections VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT (platform, account_id, collection_key) DO UPDATE SET
                type = excluded.type,
                name = CASE WHEN excluded.playlist_id IS NULL AND collections.playlist_id IS NOT NULL
                            THEN collections.name ELSE excluded.name END,
                visibility = CASE WHEN excluded.visibility = 'unknown' THEN collections.visibility ELSE excluded.visibility END,
                status = 'active', playlist_id = COALESCE(excluded.playlist_id, collections.playlist_id)
        """, (*scope, collection.type, collection.name, collection.visibility, collection.playlist_id))
        for track in tracks.values():
            save_track(db, platform, track)
        previous = defaultdict(deque)
        for row in db.execute("""
            SELECT track_id, added_at FROM memberships
            WHERE (platform, account_id, collection_key) = (?, ?, ?) ORDER BY position
        """, scope):
            previous[row['track_id']].append(row['added_at'])
        snapshot = []
        for position, member in enumerate(members):
            track_id = member.track.id
            saved = previous[track_id].popleft() if previous[track_id] else None
            snapshot.append((*scope, position, track_id, member.added_at or saved))
        db.execute("DELETE FROM memberships WHERE (platform, account_id, collection_key) = (?, ?, ?)", scope)
        db.executemany("INSERT INTO memberships VALUES (?, ?, ?, ?, ?, ?)", snapshot)


def sync(db, provider, alias, scopes, playlist_id=None):
    report = SyncReport()
    platform = provider.platform
    account_id = provider.account_id()
    with transaction(db):
        bind_account(db, platform, account_id, alias)
    selected = {}
    directory = {}
    directory_complete = False
    directory_scopes = scopes & {"owned", "subscribed"}
    if playlist_id:
        try:
            collection = provider.resolve_collection(account_id, playlist_id)
            old = db.execute("SELECT type FROM collections WHERE (platform, account_id, collection_key) = (?, ?, ?)",
                             (platform, account_id, collection.key)).fetchone()
            if collection.type != "liked":
                collection = replace(collection, type=old["type"] if old else "specified")
            selected[collection.key] = collection
        except ProviderError as error:
            report.fail(collection_selector(platform, alias, playlist_id), error)
    else:
        if "liked" in scopes:
            selected["liked"] = Collection("liked", "liked", "我喜欢的音乐")
        if directory_scopes or "liked" in scopes:
            try:
                for collection in read_pages(lambda offset: provider.list_collections(account_id, offset)):
                    if collection.key in directory:
                        raise incomplete("歌单目录包含重复 ID 或重复的红心集合。")
                    directory[collection.key] = collection
                directory_complete = bool(directory_scopes)
            except ProviderError as error:
                if directory_scopes or error.kind == "auth":
                    report.fail(collection_selector(platform, alias, "目录"), error)
                if error.kind == "auth":
                    return report
            selected.update((key, collection) for key, collection in directory.items() if collection.type in scopes)

    album_cache = {}
    for key, collection in selected.items():
        try:
            members = list(read_pages(lambda offset: provider.read_members(account_id, collection, offset)))
            tracks = complete_tracks(provider, members, db, album_cache)
            assert_identity(provider, account_id)
            save_collection(db, platform, account_id, collection, members, tracks)
        except ProviderError as error:
            report.fail(collection_selector(platform, alias, key), error)
            if error.kind == "auth":
                return report
            continue
        report.collections += 1
        report.memberships += len(members)
        report.sources.update(tracks)
        report.unresolved.update(track_id for track_id, track in tracks.items() if track.status == "unresolved")
        report.unresolved.difference_update(track_id for track_id, track in tracks.items() if track.status != "unresolved")

    if directory_complete:
        try:
            assert_identity(provider, account_id)
            with transaction(db):
                check_account(db, platform, account_id, alias)
                rows = db.execute("SELECT collection_key, type FROM collections WHERE platform = ? AND account_id = ?",
                                  (platform, account_id)).fetchall()
                db.executemany("""
                    UPDATE collections SET status = 'missing'
                    WHERE (platform, account_id, collection_key) = (?, ?, ?)
                """, ((platform, account_id, row["collection_key"]) for row in rows
                      if row["type"] in directory_scopes and row["collection_key"] not in directory))
        except ProviderError as error:
            report.fail(collection_selector(platform, alias, "目录"), error)
    report.enriched, tag_failures = enrich_track_tags(db, provider, report.sources)
    if tag_failures:
        report.fail(collection_selector(platform, alias, "曲目标签"),
                    ProviderError("metadata", f"{tag_failures} 条来源的网易云音乐百科读取未完成，可重跑同步。"))
    return report
