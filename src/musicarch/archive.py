"""Validate and exchange complete MusicArch v1 archives."""

import json
import os
from collections import defaultdict
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile

from jsonschema import Draft202012Validator, validators

from .catalog import ARRAYS, FIELDS, RELEASE_FIELDS, display_fields, normalized, source_keys, write_source
from .store import MusicArchError, encoded, transaction


def full_metadata(values):
    return {field: values.get(field, [] if field in ARRAYS else None) for field in FIELDS}


def sparse_metadata(values):
    return {field: value for field, value in values.items() if value is not None and value != []}


def group(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def read_data(db):
    accounts = [dict(row) for row in db.execute("SELECT * FROM accounts ORDER BY platform, account_id")]
    source_rows = db.execute("SELECT * FROM sources ORDER BY platform, track_id").fetchall()
    sources = [{"platform": row["platform"], "track_id": row["track_id"], "recording_id": row["recording_id"],
                "url": row["url"], "status": row["status"], "metadata": full_metadata(json.loads(row["metadata"])),
                "field_sources": json.loads(row["field_sources"])} for row in source_rows]
    collections = [{"account": [row["platform"], row["account_id"]], "key": row["collection_key"], "type": row["type"],
                    "name": row["name"], "visibility": row["visibility"], "status": row["status"],
                    "playlist_id": row["playlist_id"]}
                   for row in db.execute("SELECT * FROM collections ORDER BY platform, account_id, collection_key")]
    memberships = [{"collection": [row["platform"], row["account_id"], row["collection_key"]],
                    "source": [row["platform"], row["track_id"]], "position": row["position"], "added_at": row["added_at"]}
                   for row in db.execute("SELECT * FROM memberships ORDER BY platform, account_id, collection_key, position")]
    overrides = [{**dict(row), "value": json.loads(row["value"])} for row in db.execute(
        "SELECT * FROM overrides ORDER BY recording_id, field")]
    tags = [{"recording_id": row["recording_id"], "dimension": row["dimension"], "value": row["norm"], "label": row["label"],
             "origin": "manual", "action": row["action"]} for row in db.execute(
        "SELECT * FROM tags ORDER BY recording_id, dimension, norm")]
    separations = [{"left": list(row)[:2], "right": list(row)[2:]} for row in db.execute("SELECT * FROM separations ORDER BY 1, 2, 3, 4")]
    redirects = [dict(row) for row in db.execute("SELECT * FROM redirects ORDER BY old_id")]
    rows_by_id = group(source_rows, lambda row: row["recording_id"])
    edits_by_id = group(overrides, lambda row: row["recording_id"])
    recordings = []
    for row in db.execute("SELECT id FROM recordings ORDER BY id"):
        rid = row["id"]
        metadata, origins = display_fields(rows_by_id[rid], {edit["field"]: edit["value"] for edit in edits_by_id[rid]})
        recordings.append({"id": rid, "metadata": metadata, "field_sources": origins})
    return {"format": "musicarch", "schema_version": 1, "accounts": accounts, "recordings": recordings, "sources": sources,
            "collections": collections, "memberships": memberships, "annotations": {"overrides": overrides, "tags": tags},
            "decisions": {"separations": separations, "redirects": redirects}}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def stats(db):
    counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
        "accounts", "recordings", "sources", "collections", "memberships", "tags", "overrides", "redirects", "separations")}
    counts["total"] = sum(counts.values())
    counts["manual"] = sum(counts[table] for table in ("tags", "overrides", "redirects", "separations"))
    return counts


def export_file(db, path):
    db_path = Path(db.execute("PRAGMA database_list").fetchone()[2]).resolve()
    reserved = [db_path, *(db_path.with_name(db_path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))]
    if path.resolve() in reserved:
        raise MusicArchError("导出路径与当前工作库或其日志文件相同。")
    with transaction(db, read=True):
        data = read_data(db)
        counts = stats(db)
    atomic_json(path, data)
    return counts


@lru_cache(maxsize=1)
def validator():
    schema = json.loads(files("musicarch_schemas").joinpath("musicarch-v1.schema.json").read_text(encoding="utf-8"))
    strict_integer = Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, value: type(value) is int)
    return validators.extend(Draft202012Validator, type_checker=strict_integer)(schema)


def location(parts):
    return "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)


def invalid(where, message):
    raise MusicArchError(f"{where} {message}")


def plain_json(path):
    class ObjectPairs(list):
        pass

    def materialize(value, parts):
        if isinstance(value, ObjectPairs):
            result = {}
            for key, item in value:
                child = (*parts, key)
                if key in result:
                    invalid(location(child), "对象键重复。")
                materialize(key, parts)
                result[key] = materialize(item, child)
            return result
        if isinstance(value, list):
            return [materialize(item, (*parts, index)) for index, item in enumerate(value)]
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                invalid(location(parts), "字符串编码无效。")
        return value

    def bad_constant(value):
        invalid("JSON", f"数值常量无效 {value}。")

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=ObjectPairs, parse_constant=bad_constant)
        return materialize(data, ())
    except json.JSONDecodeError as error:
        invalid(f"JSON 第 {error.lineno} 行第 {error.colno} 列", "语法无效。")
    except (UnicodeError, RecursionError, ValueError):
        invalid("JSON", "编码或数据格式无效。")


def check_scalars(data):
    if not isinstance(data, dict) or data.get("format") != "musicarch":
        invalid("$.format", "应为 'musicarch'。")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        invalid("$.schema_version", "仅支持整数版本 1。")
    error = next(validator().iter_errors(data), None)
    if error:
        where = location(error.absolute_path)
        if error.validator == "required":
            missing = next(key for key in error.validator_value if key not in error.instance)
            invalid(f"{where}.{missing}", "缺少必填键。")
        if error.validator == "additionalProperties":
            unknown = sorted(error.instance.keys() - error.schema.get("properties", {}).keys())
            invalid(f"{where}.{unknown[0]}", "未知键。")
        invalid(where, f"与 v1 约束不符（{error.validator}）。")


def unique_index(rows, key, where):
    result = {}
    for index, row in enumerate(rows):
        identifier = key(row)
        if identifier in result:
            invalid(f"{where}[{index}]", f"业务键重复 {identifier!r}。")
        result[identifier] = row
    return result


def check_date(value, where):
    if value:
        try:
            date.fromisoformat(value + {4: "-01-01", 7: "-01", 10: ""}[len(value)])
        except ValueError:
            invalid(where, "发行日期无效。")


def sections(data):
    return (data["accounts"], data["recordings"], data["sources"], data["collections"], data["memberships"],
            data["annotations"]["overrides"], data["annotations"]["tags"], data["decisions"]["separations"], data["decisions"]["redirects"])


def validate(data):
    check_scalars(data)
    accounts, recordings, sources, collections, memberships, overrides, tags, separations, redirects = sections(data)
    account_index = unique_index(accounts, lambda row: (row["platform"], row["account_id"]), "$.accounts")
    unique_index(accounts, lambda row: (row["platform"], row["alias"]), "$.accounts")
    recording_index = unique_index(recordings, lambda row: row["id"], "$.recordings")
    source_index = unique_index(sources, source_keys, "$.sources")
    collection_index = unique_index(collections, lambda row: (*row["account"], row["key"]), "$.collections")
    unique_index(memberships, lambda row: (*row["collection"], row["position"]), "$.memberships")
    unique_index(overrides, lambda row: (row["recording_id"], row["field"]), "$.annotations.overrides")
    unique_index(tags, lambda row: (row["recording_id"], row["dimension"], row["value"]), "$.annotations.tags")
    unique_index(separations, lambda row: (*row["left"], *row["right"]), "$.decisions.separations")
    unique_index(redirects, lambda row: row["old_id"], "$.decisions.redirects")

    for index, row in enumerate(sources):
        where = f"$.sources[{index}]"
        if row["recording_id"] not in recording_index:
            invalid(f"{where}.recording_id", "引用的歌曲不存在。")
        present = sparse_metadata(row["metadata"])
        if present.keys() != row["field_sources"].keys():
            invalid(f"{where}.field_sources", "每个已知来源字段应有一个取值来源。")
        check_date(row["metadata"]["release_date"], f"{where}.metadata.release_date")
    for index, row in enumerate(collections):
        where = f"$.collections[{index}]"
        if tuple(row["account"]) not in account_index:
            invalid(f"{where}.account", "引用的账号不存在。")
        if (row["type"] == "liked") != (row["key"] == "liked"):
            invalid(f"{where}.key", "红心类型应使用固定集合键 liked。")
        if row["type"] != "liked" and row["key"] != row["playlist_id"]:
            invalid(f"{where}.playlist_id", "普通歌单的集合键应为平台歌单 ID。")
    for index, row in enumerate(memberships):
        where = f"$.memberships[{index}]"
        if tuple(row["collection"]) not in collection_index:
            invalid(f"{where}.collection", "引用的集合不存在。")
        if tuple(row["source"]) not in source_index:
            invalid(f"{where}.source", "引用的来源不存在。")
        if row["source"][0] != row["collection"][0]:
            invalid(f"{where}.source", "成员来源应与集合平台一致。")
        if row["added_at"]:
            try:
                datetime.fromisoformat(row["added_at"])
            except ValueError:
                invalid(f"{where}.added_at", "收藏时间无效。")
    for key, rows in group(memberships, lambda row: tuple(row["collection"])).items():
        if sorted(row["position"] for row in rows) != list(range(len(rows))):
            invalid("$.memberships", f"集合 {key!r} 的位置必须从 0 连续排列。")
    for name, rows in (("overrides", overrides), ("tags", tags)):
        for index, row in enumerate(rows):
            where = f"$.annotations.{name}[{index}]"
            if row["recording_id"] not in recording_index:
                invalid(f"{where}.recording_id", "引用的歌曲不存在。")
            if name == "tags" and (not normalized(row["label"]) or normalized(row["label"]) != row["value"]):
                invalid(f"{where}.value", "标签值与规范化的标签名不一致。")
            if name == "overrides" and row["field"] == "release_date":
                check_date(row["value"], f"{where}.value")
    for index, row in enumerate(separations):
        where = f"$.decisions.separations[{index}]"
        left, right = tuple(row["left"]), tuple(row["right"])
        for side, key in (("left", left), ("right", right)):
            if key not in source_index:
                invalid(f"{where}.{side}", "引用的来源不存在。")
        if left >= right or source_index[left]["recording_id"] == source_index[right]["recording_id"]:
            invalid(where, "分开决定应按来源键排序，并指向两个不同歌曲。")
    for index, row in enumerate(redirects):
        where = f"$.decisions.redirects[{index}]"
        if row["old_id"] in recording_index:
            invalid(f"{where}.old_id", "旧 ID 仍是有效歌曲 ID。")
        if row["recording_id"] not in recording_index:
            invalid(f"{where}.recording_id", "目标歌曲不存在。")

    rows_by_id = group(sources, lambda row: row["recording_id"])
    edits_by_id = group(overrides, lambda row: row["recording_id"])
    for index, row in enumerate(recordings):
        where = f"$.recordings[{index}]"
        rid = row["id"]
        rows = [{**source, "metadata": encoded(sparse_metadata(source["metadata"])), "field_sources": encoded(source["field_sources"])}
                for source in rows_by_id[rid]]
        edits = {edit["field"]: edit["value"] for edit in edits_by_id[rid]}
        metadata, origins = display_fields(rows, edits)
        for field in FIELDS:
            if row["metadata"][field] != metadata[field]:
                invalid(f"{where}.metadata.{field}", "展示取值与来源和人工修订不一致。")
            if row["field_sources"].get(field) != origins.get(field):
                invalid(f"{where}.field_sources.{field}", "展示来源与来源记录和人工修订不一致。")


def conflict(kind, key):
    raise MusicArchError(f"导入冲突，{kind} {key!r}。可用 --db 导入独立库查看。")


def import_accounts(db, rows):
    for row in rows:
        key = (row["platform"], row["account_id"])
        # Existing aliases are local labels. New accounts cannot take an occupied alias.
        if not db.execute("SELECT 1 FROM accounts WHERE (platform, account_id) = (?, ?)", key).fetchone():
            if db.execute("SELECT 1 FROM accounts WHERE (platform, alias) = (?, ?)", (key[0], row["alias"])).fetchone():
                conflict("账号别名", (key[0], row["alias"]))
            db.execute("INSERT INTO accounts VALUES (?, ?, ?)", (*key, row["alias"]))


def import_sources(db, rows):
    for row in rows:
        key = source_keys(row)
        old = db.execute("SELECT * FROM sources WHERE (platform, track_id) = (?, ?)", key).fetchone()
        values = sparse_metadata(row["metadata"])
        origins = row["field_sources"]
        url, status = row["url"], row["status"]
        if old:
            if old["recording_id"] != row["recording_id"]:
                conflict("来源歌曲归属", key)
            kept = json.loads(old["metadata"])
            if kept.get("album_id") and values.get("album_id") and kept["album_id"] != values["album_id"]:
                for field in RELEASE_FIELDS:
                    values.pop(field, None)
            values.update(kept)
            origins = {field: origin for field, origin in origins.items() if field in values} | json.loads(old["field_sources"])
            url = old["url"] or url
            status = old["status"] if old["status"] != "unresolved" else status
        write_source(db, *key, row["recording_id"], url, status, values, origins)


def import_collections(db, collections, memberships):
    by_collection = group(memberships, lambda row: tuple(row["collection"]))
    for row in collections:
        key = (*row["account"], row["key"])
        incoming = sorted((member["position"], member["source"][1], member["added_at"]) for member in by_collection[key])
        if db.execute("SELECT 1 FROM collections WHERE (platform, account_id, collection_key) = (?, ?, ?)", key).fetchone():
            existing = [tuple(member) for member in db.execute("""
                SELECT position, track_id, added_at FROM memberships
                WHERE (platform, account_id, collection_key) = (?, ?, ?) ORDER BY position
            """, key)]
            if existing != incoming:
                conflict("集合成员、顺序或收藏时间", key)
        else:
            db.execute("INSERT INTO collections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (*key, row["type"], row["name"], row["visibility"], row["status"], row["playlist_id"]))
            db.executemany("INSERT INTO memberships VALUES (?, ?, ?, ?, ?, ?)", ((*key, *member) for member in incoming))


def import_annotations(db, overrides, tags):
    for row in overrides:
        key = (row["recording_id"], row["field"])
        old = db.execute("SELECT value FROM overrides WHERE (recording_id, field) = (?, ?)", key).fetchone()
        if old and json.loads(old[0]) != row["value"]:
            conflict("人工字段", key)
        db.execute("INSERT OR IGNORE INTO overrides VALUES (?, ?, ?)", (*key, encoded(row["value"])))
    for row in tags:
        key = (row["recording_id"], row["dimension"], row["value"])
        old = db.execute("SELECT action FROM tags WHERE (recording_id, dimension, norm) = (?, ?, ?)", key).fetchone()
        if old and old[0] != row["action"]:
            conflict("人工标签决定", key)
        db.execute("INSERT OR IGNORE INTO tags VALUES (?, ?, ?, ?, ?)", (*key, row["label"], row["action"]))


def import_decisions(db, separations, redirects):
    for row in separations:
        key = (*row["left"], *row["right"])
        db.execute("INSERT OR IGNORE INTO separations VALUES (?, ?, ?, ?)", key)
    for row in redirects:
        key = (row["old_id"],)
        old = db.execute("SELECT recording_id FROM redirects WHERE old_id = ?", key).fetchone()
        if (old and old[0] != row["recording_id"]) or db.execute("SELECT 1 FROM recordings WHERE id = ?", key).fetchone():
            conflict("歌曲合并决定", row["old_id"])
        db.execute("INSERT OR IGNORE INTO redirects VALUES (?, ?)", (*key, row["recording_id"]))


def import_file(db, path):
    data = plain_json(path)
    validate(data)
    accounts, recordings, sources, collections, memberships, overrides, tags, separations, redirects = sections(data)
    with transaction(db):
        initial = stats(db)["total"]
        import_accounts(db, accounts)
        for row in recordings:
            if db.execute("SELECT 1 FROM redirects WHERE old_id = ?", (row["id"],)).fetchone():
                conflict("歌曲 ID 已合并", row["id"])
            db.execute("INSERT OR IGNORE INTO recordings VALUES (?)", (row["id"],))
        import_sources(db, sources)
        import_collections(db, collections, memberships)
        import_annotations(db, overrides, tags)
        import_decisions(db, separations, redirects)
        counts = stats(db)
        counts["new"] = counts["total"] - initial
    return counts
