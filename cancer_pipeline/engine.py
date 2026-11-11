"""Durable bounded inference. Study prompts and output validation are supplied by callers."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import time
import zlib
from .gateway import make_payload


def setup(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS notes (
        position INTEGER PRIMARY KEY, case_id TEXT UNIQUE, source_file TEXT, source_row INTEGER,
        mrn TEXT, note_id TEXT, signed TEXT, status TEXT, result BLOB, usd REAL
    )"""
    )
    if "reserved_usd" not in {r[1] for r in db.execute("PRAGMA table_info(notes)")}:
        db.execute("ALTER TABLE notes ADD COLUMN reserved_usd REAL NOT NULL DEFAULT 0")
    db.execute("CREATE INDEX IF NOT EXISTS by_mrn ON notes (mrn, position)")
    db.commit()


def add(db, position, case, status):
    m = case.get("ledger_metadata", case["input"]["metadata"])
    db.execute(
        "INSERT INTO notes (position,case_id,source_file,source_row,mrn,note_id,signed,status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            position,
            case["case_id"],
            case["source_file"],
            case["source_row"],
            m["pat_mrn_id"],
            m["note_id"],
            m["note_signed_time"],
            status,
        ),
    )


def finish(db, position, status, result):
    db.execute(
        "UPDATE notes SET status=?, result=?, usd=?, reserved_usd=? WHERE position=?",
        (
            status,
            zlib.compress(json.dumps(result, ensure_ascii=False).encode()),
            result.get("estimated_usd"),
            result.get("reserved_usd", 0),
            position,
        ),
    )


def infer(
    db,
    cases,
    prompt,
    rates,
    budget,
    workers,
    worker,
    checkpoint,
    reused=None,
    *,
    model_version,
    completion_cap,
    reasoning_effort,
    stop_event,
    text_key="full_text",
):
    hard_stops = ("inflight", "invalid_usage", "usage_over_reserve", "http_error")
    placeholders = ",".join("?" for _ in hard_stops)
    if db.execute(
        f"SELECT 1 FROM notes WHERE status IN ({placeholders}) LIMIT 1", hard_stops
    ).fetchone():
        return "paused_reconciliation"
    spent = db.execute("SELECT COALESCE(SUM(usd), 0) FROM notes").fetchone()[0]
    finished = db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    held = db.execute("SELECT COALESCE(SUM(reserved_usd),0) FROM notes").fetchone()[0]
    consecutive_unknown = 0
    initial = finished
    anomaly_count = db.execute(
        "SELECT COUNT(*) FROM notes WHERE status NOT IN ('ok', 'reused', 'empty_text')"
    ).fetchone()[0]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active = {}
        reserve = 0.0
        stop_reason = None
        cooldown_until = 0.0

        def collect():
            nonlocal spent, reserve, finished, stop_reason, anomaly_count, held, consecutive_unknown, cooldown_until
            if not active:
                return
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                position, quote = active.pop(future)
                status, result = future.result()
                with db:
                    finish(db, position, status, result)
                reserve -= quote
                spent += result.get("estimated_usd") or 0
                held += result.get("reserved_usd", 0)
                consecutive_unknown = (
                    consecutive_unknown + 1
                    if status in ("request_outcome_unknown", "gateway_server_error")
                    else 0
                )
                if consecutive_unknown >= 3:
                    stop_reason = "paused_transport_errors"
                finished += 1
                anomaly_count += status != "ok"
                if status == "rate_limited":
                    cooldown_until = max(
                        cooldown_until, time.monotonic() + result["retry_after_seconds"]
                    )
                if status in hard_stops:
                    stop_reason = "paused_reconciliation"
                if finished == initial + 1 or finished % 100 == 0:
                    checkpoint("running", finished, spent)
            if not active:
                reserve = 0.0
            if finished >= 50 and anomaly_count / finished >= 0.1 and not stop_reason:
                stop_reason = "paused_error_rate"

        for position, case in enumerate(cases, 1):
            if stop_event.is_set() or stop_reason:
                break
            if cooldown_until > time.monotonic():
                if stop_event.wait(cooldown_until - time.monotonic()):
                    break
                cooldown_until = 0.0
            previous = db.execute(
                "SELECT case_id FROM notes WHERE position=?", (position,)
            ).fetchone()
            if previous:
                if previous[0] != case["case_id"]:
                    raise ValueError("Source row mismatch on resume")
                continue
            prior = (reused or {}).get(case["case_id"])
            if prior:
                if prior[0] != case["input"]:
                    raise ValueError("Canary source row no longer matches")
                with db:
                    add(db, position, case, "reused")
                    finish(db, position, "reused", dict(prior[1], estimated_usd=0))
                finished += 1
                continue
            payload = make_payload(
                model_version, prompt, case["input"], completion_cap, reasoning_effort
            )
            limit = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
            if not case["input"][text_key].strip() or limit > 256_000:
                status = (
                    "empty_text"
                    if not case["input"][text_key].strip()
                    else "oversized_input"
                )
                with db:
                    add(db, position, case, status)
                finished += 1
                anomaly_count += status == "oversized_input"
                continue
            quote = (
                limit * rates["input"] + completion_cap * rates["output"]
            ) / 1_000_000
            while (
                active
                and spent + held + reserve + quote > budget
                and not stop_reason
                and not stop_event.is_set()
            ):
                collect()
            if stop_reason or stop_event.is_set():
                break
            if spent + held + reserve + quote > budget:
                stop_reason = "paused_budget"
                break
            with db:
                add(db, position, case, "inflight")
            active[pool.submit(worker, case, payload, limit)] = (position, quote)
            reserve += quote
            if len(active) >= workers:
                collect()
        while active:
            collect()
    return stop_reason or ("paused" if stop_event.is_set() else "complete")
