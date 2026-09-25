"""Per-field translations of a script into one target language.

Translations live in the ``translations`` table, keyed by
``(script_id, lang, node_id, option_id, field)`` — always by node/option ID,
never by position, so a deleted node/option can never pass its old
translation on to a newly created object (new objects get fresh IDs).

Each row snapshots ``source_text``: the original text the translation was
last edited or confirmed against. When the original field drifts away from
that snapshot the translation becomes *stale* (待复核) and answer pages must
fall back to the original text for that field. This module is pure logic —
no FastAPI imports — mirroring graph.py's separation.
"""
from __future__ import annotations

from typing import Any

NODE_FIELDS = ("title", "text")     # node-level fields (option_id == '')
OPTION_FIELD = "label"              # option-level field (option_id set)
ALL_FIELDS = NODE_FIELDS + (OPTION_FIELD,)


def source_text(content: dict[str, Any], node_id: str, option_id: str,
                field: str) -> str | None:
    """Original text of one translatable field; None if the object is gone."""
    node = next((n for n in content.get("nodes", []) or []
                 if n.get("id") == node_id), None)
    if node is None:
        return None
    if field in NODE_FIELDS and not option_id:
        return node.get(field) or ""
    if field == OPTION_FIELD and option_id:
        opt = next((o for o in node.get("options", []) or []
                    if o.get("id") == option_id), None)
        return (opt.get("label") or "") if opt is not None else None
    return None


def sync_translations(db, script_id: int, lang: str,
                      content: dict[str, Any], ts: str) -> None:
    """Reconcile stored translations with the just-saved script content.

    * Rows whose node/option vanished are deleted, so stale translations of
      removed objects can never resurface on different objects later.
    * Rows whose original text no longer matches the stored ``source_text``
      snapshot are flagged stale (待复核); rows that match again (e.g. the
      original was reverted) automatically clear the flag.
    """
    if not lang:
        return
    rows = db.execute(
        "SELECT node_id, option_id, field, source_text, stale FROM translations "
        "WHERE script_id = ? AND lang = ?",
        (script_id, lang),
    ).fetchall()
    for r in rows:
        src = source_text(content, r["node_id"], r["option_id"], r["field"])
        if src is None:
            db.execute(
                "DELETE FROM translations WHERE script_id = ? AND lang = ? "
                "AND node_id = ? AND option_id = ? AND field = ?",
                (script_id, lang, r["node_id"], r["option_id"], r["field"]),
            )
            continue
        stale = 0 if r["source_text"] == src else 1
        if stale != r["stale"]:
            db.execute(
                "UPDATE translations SET stale = ?, updated_at = ? "
                "WHERE script_id = ? AND lang = ? AND node_id = ? "
                "AND option_id = ? AND field = ?",
                (stale, ts, script_id, lang, r["node_id"], r["option_id"],
                 r["field"]),
            )


def freeze_snapshot(db, script_id: int, lang: str,
                    content: dict[str, Any]) -> dict[str, Any]:
    """Freeze the translations usable RIGHT NOW into a share-snapshot map.

    Only reviewed (non-stale), non-empty translations whose stored source
    snapshot still matches the saved original are frozen. Missing or stale
    fields are simply absent, so the answer page falls back to the original
    text field by field. The result is a plain JSON-able dict::

        {"lang": "en", "fields": {"<node_id>": {"title": ..., "text": ...},
                                  "<node_id>#<option_id>": {"label": ...}}}
    """
    rows = db.execute(
        "SELECT node_id, option_id, field, text, source_text, stale "
        "FROM translations WHERE script_id = ? AND lang = ?",
        (script_id, lang),
    ).fetchall()
    fields: dict[str, dict[str, str]] = {}
    for r in rows:
        text = (r["text"] or "").strip()
        if r["stale"] or not text:
            continue
        src = source_text(content, r["node_id"], r["option_id"], r["field"])
        if src is None or src != r["source_text"]:
            continue  # vanished, or went stale since the last save
        key = r["node_id"] if not r["option_id"] else f"{r['node_id']}#{r['option_id']}"
        fields.setdefault(key, {})[r["field"]] = r["text"]
    return {"lang": lang, "fields": fields}


def translation_stats(db, script_id: int, lang: str,
                      content: dict[str, Any]) -> dict[str, int]:
    """Live ok/stale/missing counts of every translatable field of *content*.

    Used by the editor when offering a translated share: it shows how many
    fields would be frozen and how many would fall back to the original.
    """
    rows = db.execute(
        "SELECT node_id, option_id, field, text, source_text FROM translations "
        "WHERE script_id = ? AND lang = ?",
        (script_id, lang),
    ).fetchall()
    saved = {(r["node_id"], r["option_id"], r["field"]): r for r in rows}
    stats = {"ok": 0, "stale": 0, "missing": 0, "total": 0}

    def account(node_id: str, option_id: str, field: str, src: str) -> None:
        stats["total"] += 1
        row = saved.get((node_id, option_id, field))
        if row is None or not (row["text"] or "").strip():
            stats["missing"] += 1
        elif (row["source_text"] or "") != src:
            stats["stale"] += 1
        else:
            stats["ok"] += 1

    for node in content.get("nodes", []) or []:
        nid = node.get("id")
        account(nid, "", "title", node.get("title") or "")
        account(nid, "", "text", node.get("text") or "")
        for opt in node.get("options", []) or []:
            account(nid, opt.get("id"), OPTION_FIELD, opt.get("label") or "")
    return stats
