"""Human feedback loop: record verdicts, promote readings to reference examples,
and select examples for few-shot calibration in analyze_images."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from wlm import db, settings

logger = logging.getLogger("wlm.learning")

_VALID_VERDICTS = frozenset({"correct", "wrong"})
_VALID_STATUSES = frozenset({"normal", "warning", "critical"})


def record_feedback(
    reading_id: int,
    verdict: str,
    true_status: str | None = None,
    true_level_index: float | None = None,
    note: str | None = None,
    as_example: bool = False,
    conn=None,
) -> None:
    """Upsert one feedback row for the given reading.

    For verdict 'correct', true_status and true_level_index default to the
    reading's own values.  Raises ValueError on invalid input.
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_VALID_VERDICTS)}, got {verdict!r}")

    with db._conn(conn) as c:
        row = c.execute(
            "SELECT status, level_index FROM readings WHERE id = ?", (reading_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"No reading with id={reading_id}")

        if verdict == "correct":
            if true_status is None:
                true_status = row["status"]
            if true_level_index is None:
                true_level_index = row["level_index"]
        else:
            # wrong verdict requires a true_status
            if true_status is None:
                raise ValueError("true_status is required when verdict is 'wrong'")

        if true_status is not None and true_status not in _VALID_STATUSES:
            raise ValueError(
                f"true_status must be one of {sorted(_VALID_STATUSES)}, got {true_status!r}"
            )

        if true_level_index is not None:
            true_level_index = max(0.0, min(100.0, float(true_level_index)))

        # Determine previous example state for cleanup
        existing = c.execute(
            "SELECT is_example, example_dir FROM feedback WHERE reading_id = ?", (reading_id,)
        ).fetchone()
        was_example = existing is not None and existing["is_example"]
        old_example_dir = existing["example_dir"] if existing else None

        example_dir: str | None = None
        if as_example:
            example_dir = _copy_example_images(reading_id, c)
        elif was_example and old_example_dir:
            _delete_example_folder(old_example_dir)

        c.execute(
            """INSERT INTO feedback
               (reading_id, ts, verdict, true_status, true_level_index, note, is_example, example_dir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(reading_id) DO UPDATE SET
                 ts = excluded.ts,
                 verdict = excluded.verdict,
                 true_status = excluded.true_status,
                 true_level_index = excluded.true_level_index,
                 note = excluded.note,
                 is_example = excluded.is_example,
                 example_dir = excluded.example_dir
            """,
            (
                reading_id,
                db.now_iso(),
                verdict,
                true_status,
                true_level_index,
                note or None,
                1 if as_example else 0,
                example_dir,
            ),
        )
        c.commit()


def _copy_example_images(reading_id: int, c) -> str | None:
    """Copy the image(s) Claude saw for each ok lens into examples/<reading_id>/.

    Returns the example_dir relative to snapshot_dir(), or None on failure.
    """
    snap_dir = db.snapshot_dir()
    lens_rows = c.execute(
        "SELECT label, snapshot_path, ok FROM lens_readings WHERE reading_id = ? AND ok = 1",
        (reading_id,),
    ).fetchall()

    if not lens_rows:
        return None

    example_rel = f"examples/{reading_id}"
    example_abs = snap_dir / example_rel
    example_abs.mkdir(parents=True, exist_ok=True)

    for lr in lens_rows:
        label = lr["label"]
        snapshot_path = lr["snapshot_path"]
        if not snapshot_path:
            continue

        raw_path = snap_dir / snapshot_path
        # Use the overlay image Claude saw when it exists
        stem = raw_path.stem
        overlay = raw_path.parent / f"{stem}-lines.jpg"
        src = overlay if overlay.exists() else raw_path

        if not src.exists():
            logger.warning("Example image not found for lens %r: %s", label, src)
            continue

        dst = example_abs / f"{label}.jpg"
        try:
            shutil.copy2(str(src), str(dst))
        except OSError as exc:
            logger.warning("Failed to copy example image for lens %r: %s", label, exc)

    return example_rel


def _delete_example_folder(example_dir: str) -> None:
    snap_dir = db.snapshot_dir()
    folder = snap_dir / example_dir
    if folder.exists():
        try:
            shutil.rmtree(str(folder))
        except OSError as exc:
            logger.warning("Failed to remove example folder %s: %s", folder, exc)


def remove_example(reading_id: int, conn=None) -> None:
    """Delete the example image folder and mark is_example=0."""
    with db._conn(conn) as c:
        row = c.execute(
            "SELECT example_dir FROM feedback WHERE reading_id = ?", (reading_id,)
        ).fetchone()
        if row and row["example_dir"]:
            _delete_example_folder(row["example_dir"])
        c.execute(
            "UPDATE feedback SET is_example = 0, example_dir = NULL WHERE reading_id = ?",
            (reading_id,),
        )
        c.commit()


def get_feedback(reading_id: int, conn=None) -> dict | None:
    """Return the feedback row for a reading, or None if none exists."""
    with db._conn(conn) as c:
        row = c.execute(
            "SELECT * FROM feedback WHERE reading_id = ?", (reading_id,)
        ).fetchone()
        return dict(row) if row else None


def list_examples(conn=None) -> list[dict]:
    """Return all rows where is_example=1, newest first."""
    with db._conn(conn) as c:
        rows = c.execute(
            "SELECT * FROM feedback WHERE is_example = 1 ORDER BY ts DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def select_examples(
    conn=None,
    current_is_night: bool | None = None,
    k: int | None = None,
) -> list[dict]:
    """Return up to k examples whose files still exist, for few-shot calibration.

    Selection order: first pick one per true_status (critical, warning, normal),
    preferring same day/night as current when current_is_night is not None,
    then fill remaining slots newest-first.  Deterministic.
    """
    if k is None:
        with db._conn(conn) as c:
            k = settings.get_int("learning_max_examples", conn=c)

    if k == 0:
        return []

    with db._conn(conn) as c:
        rows = c.execute(
            """SELECT f.reading_id, f.true_status, f.true_level_index, f.note,
                      f.example_dir, f.ts,
                      MAX(lr.is_night) AS is_night
               FROM feedback f
               LEFT JOIN lens_readings lr ON lr.reading_id = f.reading_id AND lr.ok = 1
               WHERE f.is_example = 1
               GROUP BY f.reading_id
               ORDER BY f.ts DESC"""
        ).fetchall()

    snap_dir = db.snapshot_dir()
    candidates = []
    for r in rows:
        example_dir = r["example_dir"]
        if not example_dir:
            continue
        folder = snap_dir / example_dir
        images = sorted(folder.glob("*.jpg")) if folder.exists() else []
        if not images:
            continue  # files gone
        candidates.append({
            "reading_id": r["reading_id"],
            "true_status": r["true_status"],
            "true_level_index": r["true_level_index"],
            "note": r["note"],
            "images": [(p.stem, p) for p in images],
            "is_night": bool(r["is_night"]),
            "ts": r["ts"],
        })

    if not candidates:
        return []

    def night_score(ex: dict) -> int:
        if current_is_night is None:
            return 0
        return 0 if ex["is_night"] == current_is_night else 1

    # Pick one per status in priority order
    selected_ids: set[int] = set()
    selected: list[dict] = []
    for status in ("critical", "warning", "normal"):
        pool = [e for e in candidates if e["true_status"] == status]
        if not pool:
            continue
        # prefer same day/night, then newest (already sorted newest-first)
        pool.sort(key=lambda e: (night_score(e), -candidates.index(e)))
        best = pool[0]
        selected.append(best)
        selected_ids.add(best["reading_id"])
        if len(selected) >= k:
            break

    # Fill remaining slots newest first
    for ex in candidates:
        if len(selected) >= k:
            break
        if ex["reading_id"] not in selected_ids:
            selected.append(ex)
            selected_ids.add(ex["reading_id"])

    return selected[:k]
