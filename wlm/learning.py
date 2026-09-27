"""Human feedback loop: record verdicts, promote readings to reference examples,
and select examples for few-shot calibration in analyze_images."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone

from wlm import db, settings
from wlm.analysis import normalize_confidence

logger = logging.getLogger("wlm.learning")

_VALID_VERDICTS = frozenset({"correct", "wrong"})
_VALID_STATUSES = frozenset({"normal", "warning", "critical"})

# Confidence buckets: (lower_inclusive, upper_exclusive).
# The last bucket's upper bound is 1.01 so that confidence == 1.0 is included.
CONFIDENCE_BUCKETS = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)]

# Minimum age a reading must have before its example is eligible for selection.
# Prevents a freshly-made correction from immediately anchoring Claude's output.
EXAMPLE_MIN_AGE_HOURS = 6


def calibration_table(conn=None, min_samples: int = 5) -> list[dict]:
    """Return calibration accuracy per confidence bucket.

    Each row: {lo, hi, n, n_correct, accuracy, reliable}.
    Only readings with a non-unknown status and non-NULL confidence are counted.
    "Correct" means readings.status == feedback.true_status.
    """
    with db._conn(conn) as c:
        rows = c.execute(
            """SELECT r.confidence, r.status, f.true_status
               FROM readings r
               JOIN feedback f ON f.reading_id = r.id
               WHERE r.status != 'unknown'
                 AND r.confidence IS NOT NULL"""
        ).fetchall()

    result = []
    for lo, hi in CONFIDENCE_BUCKETS:
        bucket_rows = [r for r in rows if lo <= normalize_confidence(r["confidence"]) < hi]
        n = len(bucket_rows)
        n_correct = sum(1 for r in bucket_rows if r["status"] == r["true_status"])
        accurate = n_correct / n if n > 0 else None
        result.append({
            "lo": lo,
            "hi": hi,
            "n": n,
            "n_correct": n_correct,
            "accuracy": accurate,
            "reliable": n >= min_samples,
        })
    return result


def calibrate(confidence, table: list[dict]) -> float | None:
    """Return the calibrated confidence for a raw confidence value.

    Returns None when confidence is None.
    When the matching bucket is reliable (n >= min_samples), returns the bucket
    accuracy; otherwise returns the raw confidence as a fallback.
    """
    confidence = normalize_confidence(confidence)
    if confidence is None:
        return None
    for row in table:
        if row["lo"] <= confidence < row["hi"]:
            if row["reliable"] and row["accuracy"] is not None:
                return row["accuracy"]
            return confidence
    # Clamp: confidence == 1.0 is caught by the last bucket (hi=1.01), but guard anyway.
    return confidence


def accuracy_stats(conn=None) -> dict:
    """Return a summary of feedback accuracy statistics.

    Keys:
      feedback_count   - total rows in the feedback table
      status_accuracy  - fraction where readings.status == feedback.true_status (or None)
      level_mae        - mean |level_index - true_level_index| where both present (or None)
      calibration      - the list returned by calibration_table()
    """
    with db._conn(conn) as c:
        count_row = c.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()
        feedback_count = count_row["n"] if count_row else 0

        matched_rows = c.execute(
            """SELECT r.status, f.true_status, r.level_index, f.true_level_index
               FROM readings r
               JOIN feedback f ON f.reading_id = r.id
               WHERE r.status != 'unknown'"""
        ).fetchall()

    if matched_rows:
        n_status = len(matched_rows)
        n_correct = sum(1 for r in matched_rows if r["status"] == r["true_status"])
        status_accuracy: float | None = n_correct / n_status
    else:
        status_accuracy = None

    level_errors = [
        abs(r["level_index"] - r["true_level_index"])
        for r in matched_rows
        if r["level_index"] is not None and r["true_level_index"] is not None
    ]
    level_mae: float | None = sum(level_errors) / len(level_errors) if level_errors else None

    table = calibration_table(conn=conn)

    return {
        "feedback_count": feedback_count,
        "status_accuracy": status_accuracy,
        "level_mae": level_mae,
        "calibration": table,
    }


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
            if example_dir is None:
                raise ValueError(
                    "Cannot use this reading as an example: its snapshot images are no longer available"
                )
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

    Returns the example_dir relative to snapshot_dir(), or None when no image could be copied.
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

    copied = 0
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
            copied += 1
        except OSError as exc:
            logger.warning("Failed to copy example image for lens %r: %s", label, exc)

    if copied == 0:
        shutil.rmtree(str(example_abs), ignore_errors=True)
        return None
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


def recent_context(
    conn=None,
    n: int = 6,
    window_minutes: int = 120,
    now: datetime | None = None,
) -> str | None:
    """Return a short text summary of recent readings and rain, or None when there is nothing to say.

    Args:
        conn: optional DB connection.
        n: maximum number of readings to include.
        window_minutes: how far back to look for readings.
        now: reference time (UTC); defaults to the current UTC time. Provided for
            deterministic tests.

    The returned string lists readings chronologically (newest last) and, when
    weather data is available, a "Rain in last 3h" line.  Returns None when the
    window contains no non-unknown readings and no rain data.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    cutoff = (now - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    rain_cutoff = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    now_iso_str = now.isoformat(timespec="seconds")

    with db._conn(conn) as c:
        rows = c.execute(
            """SELECT r.ts, r.status, r.level_index, r.confidence,
                      f.verdict, f.true_status, f.true_level_index
               FROM readings r
               LEFT JOIN feedback f ON f.reading_id = r.id
               WHERE r.ts >= ? AND r.status != 'unknown'
               ORDER BY r.ts DESC
               LIMIT ?""",
            (cutoff, n),
        ).fetchall()

        rain_row = c.execute(
            """SELECT SUM(precipitation_mm) AS total
               FROM weather
               WHERE hour_ts >= ? AND hour_ts <= ?
               AND precipitation_mm IS NOT NULL""",
            (rain_cutoff, now_iso_str),
        ).fetchone()

    lines: list[str] = []
    for row in reversed(rows):
        ts_str = row["ts"]
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        age_minutes = int((now - ts).total_seconds() / 60)
        time_label = f"{ts.strftime('%H:%M')} UTC (-{age_minutes}m ago)"

        verdict = row["verdict"]
        if verdict == "wrong":
            display_status = row["true_status"] or row["status"]
            display_level = row["true_level_index"]
            annotation = " (human-corrected for that earlier image)"
        elif verdict == "correct":
            display_status = row["status"]
            display_level = row["level_index"]
            annotation = " (human-confirmed)"
        else:
            display_status = row["status"]
            display_level = row["level_index"]
            annotation = ""

        level_str = f"{display_level:.1f}" if display_level is not None else "-"
        conf = normalize_confidence(row["confidence"])
        conf_str = "-" if conf is None else f"{conf:.2f}"
        lines.append(
            f"  {time_label}  status={display_status}  level={level_str}  conf={conf_str}{annotation}"
        )

    rain_total = rain_row["total"] if rain_row else None
    rain_line = f"Rain in last 3h: {rain_total:.1f} mm" if rain_total is not None else None

    if not lines and rain_line is None:
        return None

    parts: list[str] = []
    if lines:
        parts.append("\n".join(lines))
    if rain_line:
        parts.append(rain_line)

    return "\n".join(parts)


def select_examples(
    conn=None,
    current_is_night: bool | None = None,
    k: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return up to k examples whose files still exist, for few-shot calibration.

    Only readings that are at least EXAMPLE_MIN_AGE_HOURS old (relative to now)
    are eligible.  This prevents a freshly-made correction from immediately
    anchoring Claude's output to the corrected label.

    Selection order: first pick one per true_status (critical, warning, normal),
    preferring same day/night as current when current_is_night is not None,
    then fill remaining slots newest-first.  Deterministic.

    Args:
        conn: optional DB connection.
        current_is_night: when not None, prefer examples with matching day/night.
        k: maximum number of examples to return; defaults to the
            learning_max_examples setting.
        now: reference time (UTC); defaults to datetime.now(timezone.utc).
            Provided for deterministic tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if k is None:
        with db._conn(conn) as c:
            k = settings.get_int("learning_max_examples", conn=c)

    if k == 0:
        return []

    cutoff = (now - timedelta(hours=EXAMPLE_MIN_AGE_HOURS)).isoformat(timespec="seconds")

    with db._conn(conn) as c:
        rows = c.execute(
            """SELECT f.reading_id, f.true_status, f.true_level_index, f.note,
                      f.example_dir, f.ts,
                      MAX(lr.is_night) AS is_night
               FROM feedback f
               JOIN readings r ON r.id = f.reading_id
               LEFT JOIN lens_readings lr ON lr.reading_id = f.reading_id AND lr.ok = 1
               WHERE f.is_example = 1
                 AND r.ts <= ?
               GROUP BY f.reading_id
               ORDER BY f.ts DESC""",
            (cutoff,),
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
