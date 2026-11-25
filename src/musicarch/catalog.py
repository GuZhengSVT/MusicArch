"""Recording identity, comparison keys, field precedence and human annotations."""

import json
import re
import unicodedata
from uuid import uuid4

from .store import MusicArchError, encoded


FIELDS = ("title", "artists", "aliases", "album", "album_id", "artwork_url", "duration_ms",
          "release_date", "version", "isrc", "language", "genre")
ARRAYS = {"artists", "aliases"}
EDITABLE = ("title", "artists", "album", "release_date", "version", "language", "genre")
EDITABLE_TAGS = ("custom", "artist", "decade", "version", "language", "genre")
FIELD_PRIORITY = {"platform_detail": 0, "album_detail": 1, "collection": 2}
RELEASE_FIELDS = ("album", "album_id", "artwork_url", "release_date")

VERSION_PATTERNS = {
    "live": r"\blive\b|现场|現場|演唱会|演唱會",
    "instrumental": r"\binstrumental\b|\bkaraoke\b|伴奏",
    "remix": r"\bremix\b|重混|混音版",
    "cover": r"\bcover\b|翻唱",
    "rerecord": r"\bre[ -]?record(?:ed|ing)?\b|重录|重錄|taylor[’']s version",
    "acoustic": r"\bacoustic\b|不插电|不插電",
    "demo": r"\bdemo\b|样带|樣帶",
    "edit": r"\bradio edit\b|\bextended mix\b",
    "remaster": r"\bremaster(?:ed)?\b|重制版|重製版",
}
VERSION_LABELS = {"live": "Live", "instrumental": "伴奏", "remix": "Remix", "cover": "翻唱", "rerecord": "重录",
                  "acoustic": "Acoustic", "demo": "Demo", "edit": "Edit", "remaster": "Remaster"}


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def version_kind(text):
    text = normalized(text)
    return tuple(key for key, pattern in VERSION_PATTERNS.items() if re.search(pattern, text))


def title_parts(title):
    text = unicodedata.normalize("NFKC", title or "").strip()
    match = re.search(r"\(([^()]*)\)$|\[([^\[\]]*)\]$|\s+[-–—]\s+(.+)$", text)
    if match:
        suffix = next(value for value in match.groups() if value is not None)
        if version_kind(suffix):
            original = (title or "").strip()
            # Keep the display spelling. Normalization may change the number of code points.
            raw = re.search(r"[（(]([^（(）)]*)[）)]$|[［\[]([^］\]]*)[］\]]$|\s+[-–—－]\s+(.+)$", original)
            if raw:
                suffix = next(value for value in raw.groups() if value is not None)
            return normalized(text[:match.start()]), suffix
    return normalized(text), None


def compare_keys(metadata):
    title, _ = title_parts(metadata.get("title"))
    artists = sorted({normalized(person["name"]) for person in metadata.get("artists", []) if normalized(person["name"])})
    isrc = re.sub(r"[-\s]", "", normalized(metadata.get("isrc"))).upper() or None
    return title or None, encoded(artists) if artists else None, isrc


def signature(metadata):
    explicit = metadata.get("version")
    _, suffix = title_parts(metadata.get("title"))
    explicit_kinds = version_kind(explicit)
    kinds = set(version_kind(suffix)) | set(explicit_kinds)
    if explicit and not explicit_kinds and normalized(explicit) not in {"studio", "original", "录音室", "录音室版", "原版"}:
        kinds.add(normalized(explicit))
    return kinds


def compatible(left, right, *, near=False):
    left_ms, right_ms = left.get("duration_ms"), right.get("duration_ms")
    if left_ms is not None and right_ms is not None and abs(left_ms - right_ms) > 2000:
        return False
    if near and (left_ms is None or right_ms is None):
        return False
    if signature(left) != signature(right):
        return False
    _, left_suffix = title_parts(left.get("title"))
    _, right_suffix = title_parts(right.get("title"))
    for a, b in ((left_suffix, right_suffix), (left.get("version"), right.get("version"))):
        if a and b and normalized(a) != normalized(b):
            generic = {normalized(label) for label in VERSION_LABELS.values()}
            if normalized(a) not in generic and normalized(b) not in generic:
                return False
    return True


def source_keys(row):
    return row["platform"], row["track_id"]


def overlap(db, metadata):
    title, artists, isrc = compare_keys(metadata)
    return db.execute("""
        SELECT * FROM sources WHERE isrc = ? OR (title_key = ? AND artist_key = ?)
        ORDER BY platform, track_id
    """, (isrc, title, artists))


def match_id(db, metadata):
    _, artists, isrc = compare_keys(metadata)
    if not isrc or not artists:
        return None
    rows = db.execute("SELECT * FROM sources WHERE isrc = ?", (isrc,)).fetchall()
    ids = {row["recording_id"] for row in rows}
    if len(ids) == 1 and all(row["artist_key"] == artists and compatible(metadata, json.loads(row["metadata"])) for row in rows):
        return ids.pop()
    return None


def write_source(db, platform, track_id, recording_id, url, status, metadata, provenance):
    db.execute("""
        INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (platform, track_id) DO UPDATE SET
           url = excluded.url, status = excluded.status, metadata = excluded.metadata,
           field_sources = excluded.field_sources, title_key = excluded.title_key,
           artist_key = excluded.artist_key, isrc = excluded.isrc
    """, (platform, track_id, recording_id, url, status, encoded(metadata), encoded(provenance), *compare_keys(metadata)))


def save_track(db, platform, track):
    old = db.execute("SELECT * FROM sources WHERE (platform, track_id) = (?, ?)", (platform, track.id)).fetchone()
    metadata = json.loads(old["metadata"]) if old else {}
    provenance = json.loads(old["field_sources"]) if old else {}
    new_album = track.metadata.get("album_id")
    release_conflict = False
    if new_album and metadata.get("album_id") and metadata["album_id"] != new_album:
        release_conflict = FIELD_PRIORITY[track.field_sources["album_id"]] > FIELD_PRIORITY[provenance["album_id"]]
        if not release_conflict:
            for field in RELEASE_FIELDS:
                metadata.pop(field, None)
                provenance.pop(field, None)
    for field, value in track.metadata.items():
        if release_conflict and field in RELEASE_FIELDS:
            continue
        incoming = track.field_sources[field]
        if FIELD_PRIORITY[incoming] <= FIELD_PRIORITY.get(provenance.get(field), 9):
            metadata[field] = value
            provenance[field] = incoming
    if old:
        recording_id = old["recording_id"]
    else:
        recording_id = match_id(db, metadata)
        if not recording_id:
            recording_id = str(uuid4())
            db.execute("INSERT INTO recordings VALUES (?)", (recording_id,))
    status = track.status
    if old and old["status"] == "unavailable" and status == "unresolved":
        status = "unavailable"
    url = track.url
    if old and url and track.status != "available" and not old["url"]:
        url = None
    write_source(db, platform, track.id, recording_id, url, status, metadata, provenance)


def display_fields(rows, overrides):
    metadata = [(row, json.loads(row["metadata"]), json.loads(row["field_sources"])) for row in rows]
    fields = {key: [] if key in ARRAYS else None for key in FIELDS}
    origins = {}

    def rank(item, field):
        row, values, provenance = item
        return FIELD_PRIORITY[provenance[field]], row["status"] != "available", source_keys(row)

    selected = {}
    for field in FIELDS:
        options = [item for item in metadata if item[1].get(field) is not None and field in item[2]]
        if options:
            selected[field] = min(options, key=lambda item: rank(item, field))

    anchor = selected.get("album") or selected.get("album_id")
    if anchor:
        # Keep display release fields together. The independent releases remain on their sources.
        album_id, album = anchor[1].get("album_id"), normalized(anchor[1].get("album"))
        related = [anchor]
        for item in metadata:
            if item is not anchor and item[0]["platform"] == anchor[0]["platform"]:
                if album_id and item[1].get("album_id") == album_id:
                    related.append(item)
                elif not album_id and not item[1].get("album_id") and album and normalized(item[1].get("album")) == album:
                    related.append(item)
        for field in RELEASE_FIELDS:
            options = [item for item in related if item[1].get(field) is not None and field in item[2]]
            if options:
                selected[field] = min(options, key=lambda item: rank(item, field))
            else:
                selected.pop(field, None)
    for field, (row, values, provenance) in selected.items():
        fields[field] = values[field]
        origins[field] = {"kind": provenance[field], "source": list(source_keys(row))}
    for field, value in overrides.items():
        fields[field] = value
        origins[field] = {"kind": "manual", "source": None}
    if fields["version"] is None and "version" not in overrides:
        _, suffix = title_parts(fields["title"])
        if suffix:
            fields["version"] = suffix
            origins["version"] = {"kind": "title", "source": origins["title"]["source"]}
    return fields, origins


def read_overrides(db, recording_id):
    return {row["field"]: json.loads(row["value"]) for row in db.execute(
        "SELECT field, value FROM overrides WHERE recording_id = ?", (recording_id,))}


def resolve_id(db, recording_id):
    row = db.execute("SELECT recording_id FROM redirects WHERE old_id = ?", (recording_id,)).fetchone()
    if row:
        return row["recording_id"]
    if not db.execute("SELECT 1 FROM recordings WHERE id = ?", (recording_id,)).fetchone():
        raise MusicArchError(f"找不到歌曲 {recording_id}。")
    return recording_id


def pair(left, right):
    first, second = sorted((left, right))
    return (*first, *second)


def candidates(db):
    reviewed = set()
    separated = {tuple(sorted(row)) for row in db.execute("""
        SELECT left_source.recording_id, right_source.recording_id FROM separations
        JOIN sources AS left_source ON (left_source.platform, left_source.track_id) = (left_platform, left_track)
        JOIN sources AS right_source ON (right_source.platform, right_source.track_id) = (right_platform, right_track)
    """)}
    for left in db.execute("SELECT * FROM sources WHERE isrc IS NOT NULL OR title_key IS NOT NULL ORDER BY platform, track_id"):
        metadata = json.loads(left["metadata"])
        for right in overlap(db, metadata):
            if source_keys(left) >= source_keys(right):
                continue
            ids = tuple(sorted((left["recording_id"], right["recording_id"])))
            if ids[0] == ids[1] or ids in reviewed or ids in separated:
                continue
            other = json.loads(right["metadata"])
            if left["isrc"] and left["isrc"] == right["isrc"]:
                reason = "ISRC 相同，需核对艺人、版本、时长或多个歌曲来源"
            elif (left["title_key"] and left["title_key"] == right["title_key"] and left["artist_key"]
                  and left["artist_key"] == right["artist_key"] and compatible(metadata, other, near=True)):
                reason = "歌名和艺人相同，版本相容，时长相差不超过 2 秒"
                if left["isrc"] and right["isrc"]:
                    reason += "，ISRC 不同"
            else:
                continue
            reviewed.add(ids)
            titles = (metadata.get("title"), other.get("title"))
            yield ids, titles if ids[0] == left["recording_id"] else titles[::-1], reason


def recording_sources(db, recording_id):
    return db.execute("SELECT * FROM sources WHERE recording_id = ? ORDER BY platform, track_id", (recording_id,)).fetchall()


def source_pairs(db, left, right):
    left_sources = recording_sources(db, left)
    right_sources = recording_sources(db, right)
    return [pair(source_keys(a), source_keys(b)) for a in left_sources for b in right_sources]


def separate(db, left, right):
    left, right = resolve_id(db, left), resolve_id(db, right)
    if left == right:
        raise MusicArchError("请指定两个不同的歌曲 ID。")
    pairs = source_pairs(db, left, right)
    if not pairs:
        raise MusicArchError("分开决定需要两首都有平台来源的歌曲。")
    db.executemany("INSERT OR IGNORE INTO separations VALUES (?, ?, ?, ?)", pairs)


def merge(db, keep, lose):
    keep, lose = resolve_id(db, keep), resolve_id(db, lose)
    if keep == lose:
        return keep
    first = read_overrides(db, keep)
    second = read_overrides(db, lose)
    fields = sorted(field for field in first.keys() & second.keys() if first[field] != second[field])
    if fields:
        raise MusicArchError(f"人工字段冲突 {'、'.join(fields)}。请固定字段值后重试。")
    actions = {(row["dimension"], row["norm"]): row["action"] for row in db.execute(
        "SELECT * FROM tags WHERE recording_id = ?", (keep,))}
    for row in db.execute("SELECT * FROM tags WHERE recording_id = ?", (lose,)):
        if actions.get((row["dimension"], row["norm"]), row["action"]) != row["action"]:
            raise MusicArchError(f"人工标签决定冲突 {row['dimension']}:{row['label']}。请统一标签决定后重试。")
    db.executemany("DELETE FROM separations WHERE (left_platform, left_track, right_platform, right_track) = (?, ?, ?, ?)",
                   source_pairs(db, keep, lose))
    db.execute("UPDATE sources SET recording_id = ? WHERE recording_id = ?", (keep, lose))
    db.execute("UPDATE OR IGNORE overrides SET recording_id = ? WHERE recording_id = ?", (keep, lose))
    db.execute("UPDATE OR IGNORE tags SET recording_id = ? WHERE recording_id = ?", (keep, lose))
    db.execute("DELETE FROM overrides WHERE recording_id = ?", (lose,))
    db.execute("DELETE FROM tags WHERE recording_id = ?", (lose,))
    db.execute("UPDATE redirects SET recording_id = ? WHERE recording_id = ?", (keep, lose))
    db.execute("INSERT INTO redirects VALUES (?, ?)", (lose, keep))
    db.execute("DELETE FROM recordings WHERE id = ?", (lose,))
    return keep


def edit(db, recording_id, values):
    db.executemany("""
        INSERT INTO overrides VALUES (?, ?, ?) ON CONFLICT (recording_id, field) DO UPDATE SET value = excluded.value
    """, ((recording_id, name, encoded(value)) for name, value in values.items()))


def update_tags(db, recording_id, dimension, values, action):
    for label in values:
        norm = normalized(label)
        if not norm:
            raise MusicArchError("标签不能为空。")
        db.execute("""
            INSERT INTO tags VALUES (?, ?, ?, ?, ?) ON CONFLICT (recording_id, dimension, norm)
            DO UPDATE SET label = excluded.label, action = excluded.action
        """, (recording_id, dimension, norm, label.strip(), action))


def recording_tags(fields, origins, annotations):
    tags = {}

    def add(dimension, label, source):
        norm = normalized(label)
        if norm:
            tags.setdefault((dimension, norm), {"dimension": dimension, "value": norm, "label": label, "origin": source})

    date = fields["release_date"]
    add("decade", f"{int(date[:4]) // 10 * 10} 年代" if date else "年代未知", "derived")
    for artist in fields["artists"]:
        add("artist", artist["name"], origins["artists"]["kind"])
    for dimension in ("version", "language", "genre"):
        label = fields[dimension]
        if label:
            kinds = version_kind(label) if dimension == "version" else ()
            values = [VERSION_LABELS[kind] for kind in kinds] if kinds else label.split(" / ")
            for value in values:
                add(dimension, value, origins[dimension]["kind"])
        elif dimension != "version":
            add(dimension, "未标注", "derived")
    for row in annotations:
        key = row["dimension"], row["norm"]
        tags.pop(key, None)
        if row["action"] == "add":
            add(row["dimension"], row["label"], "manual")
    return [tags[key] for key in sorted(tags)]
