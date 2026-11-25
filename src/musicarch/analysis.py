"""Aggregate the selected recordings into collection-based listening preferences."""

from collections import Counter


LANGUAGE_COLORS = ("#386351", "#6c9b83", "#d59766", "#547f9c", "#a96860", "#a39b65")
OTHER_COLOR = "#a6ada1"
UNKNOWN_COLOR = "#dce1d6"


def percentage(count, total):
    return round(100 * count / total, 1) if total else 0


def ranked(counts, limit):
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def bars(entries):
    maximum = max((value for _, value in entries), default=0)
    return [{"label": label, "count": value, "width": percentage(value, maximum)} for label, value in entries]


def tag_sets(catalog, dimension):
    labels = {tag["id"]: tag["label"] for group in catalog["tag_groups"] if group["dimension"] == dimension
              for tag in group["tags"] if tag["label"] != "未标注"}
    return [{labels[tag["id"]] for tag in row["tags"] if tag["id"] in labels} for row in catalog["recordings"]]


def tag_counts(per_recording):
    return Counter(label for labels in per_recording for label in labels)


def language_ring(languages, total):
    known = ranked(languages, len(LANGUAGE_COLORS))
    segments = [(label, count, color) for (label, count), color in zip(known, LANGUAGE_COLORS)]
    other = sum(languages.values()) - sum(count for _, count in known)
    missing = total - sum(languages.values())
    if other:
        segments.append(("其他语种", other, OTHER_COLOR))
    if missing:
        segments.append(("未标注", missing, UNKNOWN_COLOR))
    result, cumulative = [], 0
    for label, count, color in segments:
        start = percentage(cumulative, total)
        cumulative += count
        result.append({"label": label, "count": count, "percent": percentage(count, total), "color": color,
                       "start": start, "end": percentage(cumulative, total)})
    return result


def genre_heatmap(language_sets, genre_sets, genres):
    language_counts = tag_counts(language_sets)
    top_languages = [label for label, _ in ranked(language_counts, 5)]
    top_genres = [label for label, _ in genres[:5]]
    frequencies = Counter((language, genre) for languages, styles in zip(language_sets, genre_sets)
                          for language in top_languages if language in languages for genre in top_genres if genre in styles)
    peak = max(frequencies.values(), default=0)
    if not peak:
        return {"columns": [], "rows": []}
    palette = ("#f2f3ec", "#e1ebe0", "#cbdfcf", "#a9c8b4", "#86ae97", "#648f79", "#386351")
    rows = []
    for language in top_languages:
        cells = []
        for genre in top_genres:
            count = frequencies[language, genre]
            level = max(1, round(6 * count / peak)) if count else 0
            cells.append({"count": count, "color": palette[level], "dark": level == 6})
        rows.append({"label": language, "cells": cells})
    return {"columns": top_genres, "rows": rows}


def leader(caption, entries, owner_count, noun):
    if not entries:
        return {"caption": caption, "value": "暂无标注", "detail": ""}
    label, count = entries[0]
    return {"caption": caption, "value": label,
            "detail": f"{count} 首，在{noun}歌曲中占 {percentage(count, owner_count):g}%"}


def analyze(catalog, first_added):
    """Use only unique recordings present in the published collection selection."""
    recordings = catalog["recordings"]
    total = len(recordings)
    language_sets = tag_sets(catalog, "language")
    genre_sets = tag_sets(catalog, "genre")
    version_sets = tag_sets(catalog, "version")
    language_groups = Counter(" / ".join(sorted(labels)) for labels in language_sets if labels)
    genres = ranked(tag_counts(genre_sets), 10)
    era_counts = Counter(int(row["release_date"][:4]) // 10 * 10 for row in recordings if row["release_date"])
    eras = [(f"{era} 年代", count) for era, count in sorted(era_counts.items())]
    known_eras = sum(era_counts.values())
    known_genres = sum(bool(labels) for labels in genre_sets)
    years = Counter(int(date[:4]) for date in first_added.values())
    if years:
        timeline = [{"year": year, "count": years[year]} for year in range(min(years), max(years) + 1)]
    else:
        timeline = []
    artists = Counter({row["label"]: row["count"] for row in catalog["artists"]})
    return {
        "recordings": total,
        "collections": len(catalog["collections"]),
        "artists_count": len(artists),
        "highlights": [leader("最常见流派", genres, known_genres, "有流派标注的"),
                       leader("最常见语种", ranked(language_groups, 1), sum(language_groups.values()), "有语种标注的"),
                       leader("最常见发行年代", ranked(dict(eras), 1), known_eras, "已知发行年代的")],
        "genres": bars(genres), "genres_labeled": known_genres,
        "languages": language_ring(language_groups, total), "languages_labeled": sum(language_groups.values()),
        "eras": bars(eras), "eras_unknown": total - known_eras,
        "timeline": timeline, "timeline_labeled": sum(years.values()),
        "artists": bars(ranked(artists, 8)),
        "versions": bars(ranked(tag_counts(version_sets), 8)),
        "heatmap": genre_heatmap(language_sets, genre_sets, genres),
    }
