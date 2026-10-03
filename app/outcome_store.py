"""Confirmed outcomes, provisional bundles, and atomic operation receipts.

This module never calls a model, derives work from the notebook, or treats a
notification as progress. Every existing target carries an explicit revision.
All canonical writes, checkpoint effects and receipts share one transaction.
"""

import datetime as dt
import hashlib
import json
import re
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, timeutil

PROPOSAL_TTL = dt.timedelta(minutes=15)
STATES = frozenset({"active", "queued", "paused", "blocked", "completed", "dropped"})
FIELDS = ("title", "state", "importance", "reason", "deadline", "deadline_kind",
          "deadline_timezone", "next_step", "effort", "blocker", "progress", "queue_position")
LIMITS = {"title": 200, "reason": 1000, "next_step": 1000, "effort": 120,
          "blocker": 1000, "progress": 1500}


class OutcomeError(ValueError):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


class Receipt(TypedDict):
    operation_id: str
    proposal_id: str
    status: str
    outcomes: list[dict]
    checkpoints: list[dict]
    work_quiet_until: str | None
    applied_at: str


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now(value=None) -> dt.datetime:
    if value is None:
        return timeutil.utc_now()
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OutcomeError("Time must be an aware ISO date/time") from exc
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OutcomeError("Time must include an explicit timezone")
    return value.astimezone(dt.timezone.utc)


def _instant(value) -> str:
    if not isinstance(value, str):
        raise OutcomeError("Time must be an aware ISO date/time")
    return timeutil.utc_iso(_now(value))


def _text(value, name: str, limit: int, *, nullable=False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise OutcomeError(f"{name} must be non-empty text of at most {limit} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise OutcomeError(f"{name} contains unsupported control characters")
    return value.strip()


def _chat(chat_id) -> str:
    if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)):
        raise OutcomeError("A chat identity is required")
    return _text(str(chat_id), "chat_id", 200)


def _source(source_id) -> str:
    if isinstance(source_id, bool) or not isinstance(source_id, (str, int)):
        raise OutcomeError("A source message identity is required")
    return _text(str(source_id), "source_id", 300)


def parse_id(value) -> int:
    if isinstance(value, str) and re.fullmatch(r"O[1-9][0-9]*", value):
        value = int(value[1:])
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OutcomeError("Use an outcome ID such as O1")
    return value


def _revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OutcomeError("An explicit expected_revision is required for an existing outcome")
    return value


def _display(row: dict) -> dict:
    row = dict(row)
    row["ui_id"] = f"O{row['id']}"
    row["status"] = row["state"]
    row["provenance"] = json.loads(row.pop("provenance_json", "{}"))
    return row


async def notebook_fingerprint() -> str:
    """Hash raw evidence, distinguishing absent/empty, without reconciliation."""
    from . import memory_file
    values = await memory_file._notebook_snapshot()
    suppressed = await db.fetch_all(
        "SELECT normalized_content, content FROM memory_suppressions ORDER BY normalized_content"
    )
    legacy = None
    if values.get("memory_md_content") is None and values.get(memory_file.LEGACY_MIGRATION_KEY) is None:
        try:
            legacy = memory_file.MEMORY_FILE_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        # An unreadable source must not silently become evidence of no conflict.
    return hashlib.sha256(_json([values, suppressed, legacy]).encode()).hexdigest()


async def _control(chat_id: str, now: str | None = None) -> dict:
    row = await db.fetch_one("SELECT * FROM outcome_control WHERE chat_id = ?", (chat_id,))
    if row is None and now is not None:
        await db.execute(
            "INSERT OR IGNORE INTO outcome_control(chat_id, updated_at) VALUES (?, ?)", (chat_id, now),
        )
        row = await db.fetch_one("SELECT * FROM outcome_control WHERE chat_id = ?", (chat_id,))
    return row or {"chat_id": chat_id, "generation": 0, "revision": 0,
                   "work_quiet_until": None, "updated_at": None}


async def get(outcome_id, chat_id) -> dict | None:
    row = await db.fetch_one("SELECT * FROM outcomes WHERE id = ? AND chat_id = ?",
                             (parse_id(outcome_id), _chat(chat_id)))
    return _display(row) if row else None


def _proposal(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["operations"] = json.loads(result.pop("operations_json"))
    result["warnings"] = json.loads(result.pop("warnings_json"))
    result.pop("plan_json", None)
    result.pop("receipt_json", None)
    return result


def _existing_proposal(row: dict, now: dt.datetime) -> dict:
    if row["status"] not in ("pending", "applied"):
        raise OutcomeError("That message belongs to an old proposal; send a fresh request", "stale")
    if row["status"] == "pending" and row["expires_at"] <= timeutil.utc_iso(now):
        raise OutcomeError("That proposal expired; send a fresh request", "expired")
    return _proposal(row)


async def log_user_message_once(chat_id, source_id, text, channel="text") -> bool:
    """Idempotent direct activity logging, including the unanswered-contact slot.

    Keep no duplicate copy of message content in the identity ledger. A repeated
    Telegram update cannot fabricate new activity or unlock another checkpoint.
    """
    chat_id, source_id = _chat(chat_id), _source(source_id)
    if not isinstance(text, str) or not isinstance(channel, str):
        raise OutcomeError("Message text and channel must be text")
    timestamp = timeutil.utc_iso()
    try:
        results = await db.execute_batch([
            ("INSERT OR IGNORE INTO outcome_incoming_messages(chat_id, source_id, created_at) VALUES (?, ?, ?)",
             (chat_id, source_id, timestamp)),
            ("INSERT INTO conversation_log(role, content, channel, timestamp) SELECT 'user', ?, ?, ? WHERE changes() = 1 RETURNING id",
             (text, channel, timestamp)),
        ])
        return bool(results[-1])
    except Exception:
        # An acknowledgement lost after commit is already durable activity, not
        # permission to create another conversation entry on a blind retry.
        row = await db.fetch_one("SELECT source_id FROM outcome_incoming_messages WHERE chat_id = ? AND source_id = ?", (chat_id, source_id))
        if row:
            return False
        raise


async def pending(chat_id, now=None) -> dict | None:
    chat_id, current = _chat(chat_id), timeutil.utc_iso(_now(now))
    row = await db.fetch_one(
        "SELECT * FROM outcome_proposals WHERE chat_id = ? AND status = 'pending' AND expires_at > ?",
        (chat_id, current),
    )
    return _proposal(row)


async def snapshot(chat_id) -> dict:
    chat_id = _chat(chat_id)
    rows = [_display(row) for row in await db.fetch_all(
        "SELECT * FROM outcomes WHERE chat_id = ? AND state NOT IN ('completed','dropped') ORDER BY id",
        (chat_id,),
    )]
    fingerprint = await notebook_fingerprint()
    for row in rows:
        row["notebook_changed"] = row["notebook_fingerprint"] != fingerprint
    queued = sorted((row for row in rows if row["state"] == "queued"),
                    key=lambda row: (row["queue_position"] if row["queue_position"] is not None else row["id"], row["id"]))
    return {"focus": next((row for row in rows if row["state"] == "active"), None),
            "next": queued[:3], "outcomes": rows,
            "parked_count": sum(row["state"] in ("paused", "blocked") for row in rows) + max(0, len(queued) - 3),
            "control": await _control(chat_id), "pending": await pending(chat_id),
            "checkpoints": await db.fetch_all(
                "SELECT * FROM outcome_checkpoints WHERE chat_id = ? AND status = 'pending' ORDER BY due_at, id", (chat_id,)),
            "notebook_fingerprint": fingerprint,
            "notebook_changed": any(row["notebook_changed"] for row in rows)}


def _fields(fields: dict, base: dict | None = None) -> dict:
    if not isinstance(fields, dict) or not fields or set(fields) - set(FIELDS):
        raise OutcomeError("Only supported outcome fields may be changed")
    result = {name: None for name in FIELDS}
    result["state"] = "queued"
    if base:
        result.update({name: base.get(name) for name in FIELDS})
    for name, value in fields.items():
        if name in LIMITS:
            result[name] = _text(value, name, LIMITS[name], nullable=name != "title")
        elif name == "queue_position":
            if value is not None and (type(value) is not int or not 0 <= value <= 100000):
                raise OutcomeError("Queue position must be an integer between 0 and 100000, or null")
            result[name] = value
        elif name == "state":
            if not isinstance(value, str) or value not in STATES:
                raise OutcomeError("Unsupported outcome state")
            result[name] = value
        elif name == "importance":
            if value not in (None, "low", "medium", "high"):
                raise OutcomeError("Importance must be low, medium, high, or unknown")
            result[name] = value
        elif name == "deadline_kind":
            if value not in (None, "hard", "target"):
                raise OutcomeError("A deadline is either hard or a target")
            result[name] = value
        elif name == "deadline_timezone":
            if value is not None:
                try:
                    ZoneInfo(value)
                except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
                    raise OutcomeError("Deadline timezone must be a valid IANA timezone") from exc
            result[name] = value
        elif name == "deadline":
            if value is None:
                result[name] = None
            elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                try:
                    result[name] = dt.date.fromisoformat(value).isoformat()
                except ValueError as exc:
                    raise OutcomeError("Invalid date-only deadline") from exc
            else:
                result[name] = _instant(value)
    if not result["title"]:
        raise OutcomeError("An outcome needs a title")
    if fields.get("deadline", "absent") is None:
        result["deadline_kind"] = result["deadline_timezone"] = None
    if result["deadline"]:
        if result["deadline_kind"] is None:
            raise OutcomeError("Specify whether the deadline is hard or a target")
        if "T" in result["deadline"] and not result["deadline_timezone"]:
            raise OutcomeError("A timed deadline needs its IANA timezone")
        raw_deadline = fields.get("deadline")
        if isinstance(raw_deadline, str) and "T" in raw_deadline and result["deadline_timezone"]:
            supplied = dt.datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
            # Z/+00 is an absolute instant. A supplied non-UTC local offset,
            # however, must describe a real wall time in the named zone. This
            # rejects DST gaps and incorrect fold offsets without choosing one.
            if supplied.utcoffset() != dt.timedelta(0):
                local = supplied.astimezone(ZoneInfo(result["deadline_timezone"]))
                if (local.replace(tzinfo=None) != supplied.replace(tzinfo=None)
                        or local.utcoffset() != supplied.utcoffset()):
                    raise OutcomeError("Deadline wall time and offset do not match its timezone; clarify the DST time")
    elif result["deadline_kind"] is not None or result["deadline_timezone"] is not None:
        raise OutcomeError("Deadline metadata requires a deadline")
    return result


def _guard(condition: str, params: tuple = ()) -> tuple[str, tuple]:
    # CHECK failure aborts the whole batch on both SQLite and Turso.
    return ("INSERT INTO outcome_transaction_guard(value) SELECT 0 WHERE COALESCE((" + condition + "), 0) = 0", params)


def _control_guard(control: dict) -> tuple[str, tuple]:
    return _guard("EXISTS (SELECT 1 FROM outcome_control WHERE chat_id = ? AND revision = ? AND generation = ?)",
                  (control["chat_id"], control["revision"], control["generation"]))


async def _fresh_source_guard(chat_id: str, source_id: str):
    source = await db.fetch_one(
        "SELECT rowid AS sequence FROM outcome_incoming_messages WHERE chat_id = ? AND source_id = ?", (chat_id, source_id),
    )
    if source is None:
        return None  # Direct controls/tests may supply identity without chat logging.
    latest = await db.fetch_one("SELECT MAX(rowid) AS sequence FROM outcome_incoming_messages WHERE chat_id = ?", (chat_id,))
    if source["sequence"] != latest["sequence"]:
        raise OutcomeError("A newer message arrived; review its request before saving this proposal", "stale")
    return _guard("(SELECT MAX(rowid) FROM outcome_incoming_messages WHERE chat_id = ?) = ?", (chat_id, source["sequence"]))


async def is_current_source(chat_id, source_id) -> bool:
    """Read-only fast fence for replayed old messages before interpretation."""
    try:
        await _fresh_source_guard(_chat(chat_id), _source(source_id))
        return True
    except OutcomeError as exc:
        if exc.code == "stale":
            return False
        raise


async def _plan(chat_id: str, operations: list, source_id: str, source_text: str, now: dt.datetime) -> dict:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
        raise OutcomeError("A proposal must contain between one and twenty operations")
    rows = await db.fetch_all("SELECT * FROM outcomes WHERE chat_id = ?", (chat_id,))
    originals = {str(row["id"]): row for row in rows}
    working = {key: {name: row.get(name) for name in FIELDS} for key, row in originals.items()}
    expected, changed, quotes = {}, {}, {}
    normalized, checkpoints, cancellations, preview = [], [], [], []
    quiet = "unchanged"
    active = next((key for key, row in working.items() if row["state"] == "active"), None)

    def target(operation):
        value = operation.get("outcome_id")
        if isinstance(value, str) and value.startswith("$"):
            if value not in working:
                raise OutcomeError("A new-outcome reference must refer to an earlier create operation")
            if "expected_revision" in operation:
                raise OutcomeError("New-outcome references do not accept an existing revision")
            return value
        key = str(parse_id(value))
        if key not in originals:
            raise OutcomeError("That outcome does not belong to this chat", "not_found")
        revision = _revision(operation.get("expected_revision"))
        if originals[key]["revision"] != revision:
            raise OutcomeError("The outcome changed; review a fresh proposal", "stale")
        expected[key] = revision
        return key

    def focus(key):
        nonlocal active
        if active and active != key:
            if not active.startswith("$"):
                expected[active] = originals[active]["revision"]
            working[active]["state"] = "paused"
            changed.setdefault(active, set()).add("state")
            preview.append(f"Park {'O' + active if not active.startswith('$') else active}: {working[active]['title']}")
        working[key]["state"] = "active"
        changed.setdefault(key, set()).add("state")
        active = key

    for index, supplied in enumerate(operations):
        if not isinstance(supplied, dict):
            raise OutcomeError("Each operation must be an object")
        operation = dict(supplied)
        op = operation.get("op")
        common = {"op", "source_quote", "source_span"}
        keys = {
            "create": {"fields", "ref"}, "update": {"outcome_id", "expected_revision", "fields"},
            "select": {"outcome_id", "expected_revision"}, "quiet": {"until"},
            "checkpoint": {"outcome_id", "expected_revision", "due_at", "expires_at", "question", "agreement_source"},
            "cancel_checkpoint": {"checkpoint_id", "expected_revision"},
        }
        if not isinstance(op, str) or op not in keys or set(operation) - common - keys[op]:
            raise OutcomeError("Unsupported operation or operation fields")
        quote = operation.get("source_quote")
        span = operation.get("source_span")
        if span is not None:
            if (not isinstance(span, list) or len(span) != 2 or any(type(x) is not int for x in span)
                    or not 0 <= span[0] < span[1] <= len(source_text)):
                raise OutcomeError("Invalid source span")
            if quote is not None and quote != source_text[span[0]:span[1]]:
                raise OutcomeError("Source quote does not match source span")
            quote = source_text[span[0]:span[1]]
        if quote is not None:
            quote = _text(quote, "source_quote", 500)
            if quote not in source_text:
                raise OutcomeError("Source quote is absent from the direct message")
            operation["source_quote"] = quote
        operation.pop("source_span", None)
        if op == "create":
            ref = operation.get("ref", f"new{index}")
            if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", ref):
                raise OutcomeError("Invalid new-outcome reference")
            key = "$" + ref
            if key in working:
                raise OutcomeError("New-outcome references must be unique")
            values = _fields(operation.get("fields"))
            operation["fields"] = {name: values[name] for name in operation["fields"]}
            operation["ref"] = ref
            working[key] = values
            changed[key] = set(operation["fields"])
            changed[key].add("state")
            quotes[key] = quote
            if values["state"] == "active":
                focus(key)
            details = "; ".join(
                f"{name.replace('_', ' ')}: {values[name] if values[name] is not None else 'clear'}"
                for name in operation["fields"] if name not in ("title", "state")
            )
            preview.append(f"Create {values['title']} ({values['state']})" + (f"; {details}" if details else ""))
        elif op in ("update", "select"):
            key = target(operation)
            quotes[key] = quote
            if op == "update":
                values = _fields(operation.get("fields"), working[key])
                operation["fields"] = {name: values[name] for name in operation["fields"]}
                if operation.get("fields", {}).get("deadline", "absent") is None:
                    operation["fields"].update(deadline_kind=None, deadline_timezone=None)
                working[key] = values
                changed.setdefault(key, set()).update(operation["fields"])
                if values["state"] == "active":
                    focus(key)
                elif active == key:
                    active = None
                rendered = "; ".join(f"{name.replace('_', ' ')}: {values[name] if values[name] is not None else 'clear'}"
                                     for name in operation["fields"])
                preview.append(f"Update {'O' + key if not key.startswith('$') else key}: {rendered}")
            else:
                focus(key)
                preview.append(f"Focus on {working[key]['title']}")
        elif op == "quiet":
            if "until" not in operation:
                raise OutcomeError("Quiet needs an explicit until time or null to resume")
            quiet = None if operation["until"] is None else _instant(operation["until"])
            if quiet is not None and quiet <= timeutil.utc_iso(now):
                raise OutcomeError("Work quiet must end in the future")
            operation["until"] = quiet
            preview.append("Resume work check-ins" if quiet is None else f"Quiet for work check-ins until {quiet}")
        elif op == "checkpoint":
            key = target(operation)
            due = _instant(operation.get("due_at"))
            expires = _instant(operation.get("expires_at"))
            if due <= timeutil.utc_iso(now) or expires <= due or _now(expires) - _now(due) > dt.timedelta(days=1):
                raise OutcomeError("A checkpoint needs a future time and a delivery window of at most 24 hours")
            question = _text(operation.get("question"), "question", 500)
            agreement = _source(operation.get("agreement_source"))
            if agreement != source_id:
                raise OutcomeError("Checkpoint agreement must identify this direct source message")
            operation.update(due_at=due, expires_at=expires, question=question, agreement_source=agreement)
            checkpoints.append({"target": key, "operation_index": index, "due_at": due,
                                "expires_at": expires, "question": question, "agreement_source": agreement})
            preview.append(f"One check-in about {working[key]['title']} around {due} (expires {expires}): {question}")
        else:
            checkpoint_id = operation.get("checkpoint_id")
            if type(checkpoint_id) is not int or checkpoint_id <= 0:
                raise OutcomeError("Use a checkpoint integer ID")
            row = await db.fetch_one("SELECT * FROM outcome_checkpoints WHERE id = ? AND chat_id = ?", (checkpoint_id, chat_id))
            if row is None or row["status"] != "pending":
                raise OutcomeError("That checkpoint is no longer pending", "stale")
            key = target({"outcome_id": row["outcome_id"], "expected_revision": operation.get("expected_revision")})
            cancellations.append({"id": checkpoint_id, "revision": row["revision"]})
            preview.append(f"Cancel checkpoint {checkpoint_id} for {working[key]['title']}")
        normalized.append(operation)
    for checkpoint in checkpoints:
        if working[checkpoint["target"]]["state"] not in ("active", "queued", "blocked"):
            raise OutcomeError("Paused, completed, or dropped outcomes cannot get a checkpoint")
    warnings = []
    changed_ids = [int(key) for key in changed if not key.startswith("$")]
    if changed_ids:
        placeholders = ",".join("?" for _ in changed_ids)
        existing = await db.fetch_all(
            f"SELECT id FROM outcome_checkpoints WHERE chat_id = ? AND status = 'pending' AND outcome_id IN ({placeholders})",
            (chat_id, *changed_ids),
        )
        if existing:
            warnings.append(f"This change cancels {len(existing)} pending work check-in(s); resuming needs a new agreement")
    from . import memory
    for index, line in enumerate(preview):
        if await memory.filter_suppressed_text(line) != line:
            parked = re.match(r"^Park (O[1-9][0-9]*):", line)
            if parked:
                preview[index] = f"Park {parked.group(1)} (details withheld by memory controls)"
            else:
                raise OutcomeError("A proposed detail conflicts with memory-suppression controls; clarify the change", "conflict")
    return {"operations": normalized, "expected": expected, "changed": {key: sorted(value) for key, value in changed.items()},
            "values": {key: working[key] for key in changed}, "quotes": quotes,
            "checkpoints": checkpoints, "cancellations": cancellations, "quiet": quiet,
            "preview": "; ".join(preview), "warnings": warnings}


async def propose(chat_id, source_id, operations, source_text, now=None) -> dict:
    chat_id, source_id, now = _chat(chat_id), _source(source_id), _now(now)
    if not isinstance(source_text, str):
        raise OutcomeError("Direct source text is required")
    existing = await db.fetch_one("SELECT * FROM outcome_proposals WHERE chat_id = ? AND source_id = ?", (chat_id, source_id))
    if existing:
        return _existing_proposal(existing, now)
    epoch = None
    if isinstance(operations, list) and any(isinstance(op, dict) and op.get("op") == "checkpoint" for op in operations):
        from . import outcome_checkpoints
        epoch = await outcome_checkpoints.sync_delivery_state()
        if epoch is None:
            raise OutcomeError("Work check-in delivery is not enabled", "disabled")
    timestamp = timeutil.utc_iso(now)
    proposal_id = "P" + hashlib.sha256(_json([chat_id, source_id]).encode()).hexdigest()[:24]
    operation_id = "outcome:" + hashlib.sha256(_json([chat_id, source_id]).encode()).hexdigest()
    for attempt in range(4):
        control = await _control(chat_id, timestamp)
        plan = await _plan(chat_id, operations, source_id, source_text, now)
        plan["delivery_epoch"] = epoch
        if plan["checkpoints"]:
            fingerprint = await notebook_fingerprint()
            effective_quiet = control["work_quiet_until"] if plan["quiet"] == "unchanged" else plan["quiet"]
            for checkpoint in plan["checkpoints"]:
                if effective_quiet is not None and checkpoint["due_at"] < effective_quiet:
                    raise OutcomeError("The check-in conflicts with work quiet; choose a later time")
                key = checkpoint["target"]
                if not key.startswith("$") and key not in plan["values"]:
                    outcome = await db.fetch_one("SELECT notebook_fingerprint FROM outcomes WHERE id = ? AND chat_id = ?", (int(key), chat_id))
                    if not outcome or outcome["notebook_fingerprint"] != fingerprint:
                        raise OutcomeError("Notebook context changed; confirm the outcome before adding a new check-in", "conflict")
        source_guard = await _fresh_source_guard(chat_id, source_id)
        statements = [_control_guard(control)]
        if source_guard:
            statements.append(source_guard)
        statements.extend(_guard("EXISTS (SELECT 1 FROM outcomes WHERE id = ? AND chat_id = ? AND revision = ?)",
                                 (int(key), chat_id, revision)) for key, revision in plan["expected"].items())
        statements.extend([
            ("UPDATE outcome_proposals SET status = 'superseded' WHERE chat_id = ? AND status = 'pending'", (chat_id,)),
            ("UPDATE outcome_control SET revision = revision + 1, updated_at = ? WHERE chat_id = ?", (timestamp, chat_id)),
            ("INSERT INTO outcome_proposals(id, chat_id, source_id, operation_id, operations_json, plan_json, preview, warnings_json, generation, control_revision, status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
             (proposal_id, chat_id, source_id, operation_id, _json(plan["operations"]), _json(plan), plan["preview"], _json(plan["warnings"]), control["generation"], control["revision"] + 1, timestamp, timeutil.utc_iso(now + PROPOSAL_TTL))),
        ])
        try:
            await db.execute_batch(statements)
        except Exception:
            existing = await db.fetch_one("SELECT * FROM outcome_proposals WHERE id = ? AND chat_id = ?", (proposal_id, chat_id))
            if existing:
                return _existing_proposal(existing, now)
            await _fresh_source_guard(chat_id, source_id)
            latest = await _control(chat_id)
            if latest["revision"] != control["revision"] or latest["generation"] != control["generation"]:
                continue
            raise
        return _proposal(await db.fetch_one("SELECT * FROM outcome_proposals WHERE id = ?", (proposal_id,)))
    raise OutcomeError("Priorities kept changing; please try a fresh proposal", "stale")


def _selector(key: str, operation_id: str) -> tuple[str, tuple]:
    if key.startswith("$"):
        return "(SELECT id FROM outcomes WHERE creation_key = ?)", (operation_id + ":" + key,)
    return "?", (int(key),)


def _row_json(table: str, columns: tuple) -> str:
    return "json_object(" + ",".join(f"'{name}',{table}.{name}" for name in columns) + ")"


async def receipt(chat_id, operation_id: str) -> Receipt | None:
    row = await db.fetch_one(
        "SELECT receipt_json FROM outcome_proposals WHERE chat_id = ? AND status = 'applied' AND "
        "(operation_id = ? OR id = (SELECT proposal_id FROM outcome_events WHERE operation_id = ? AND chat_id = ?))",
        (_chat(chat_id), _text(operation_id, "operation_id", 200), operation_id, _chat(chat_id)),
    )
    if not row or not row["receipt_json"]:
        return None
    result = json.loads(row["receipt_json"])
    result["outcomes"] = [_display(item) for item in result["outcomes"]]
    return result


async def receipt_for_source(chat_id, source_id) -> Receipt | None:
    """Resolve duplicate create/confirm message identities after reply loss."""
    row = await db.fetch_one(
        "SELECT operation_id FROM outcome_proposals WHERE chat_id = ? AND status = 'applied' "
        "AND (source_id = ? OR confirmation_source = ?) ORDER BY applied_at DESC LIMIT 1",
        (_chat(chat_id), _source(source_id), _source(source_id)),
    )
    return await receipt(chat_id, row["operation_id"]) if row else None


async def reconcile(chat_id, operation_id: str) -> Receipt | None:
    """Read-only resolution before any retry of an uncertain operation."""
    return await receipt(chat_id, operation_id)


async def confirm(chat_id, proposal_id, confirmation_source, now=None) -> Receipt:
    chat_id, confirmation_source, now = _chat(chat_id), _source(confirmation_source), _now(now)
    proposal_id = _text(proposal_id, "proposal_id", 100)
    proposal = await db.fetch_one("SELECT * FROM outcome_proposals WHERE id = ? AND chat_id = ?", (proposal_id, chat_id))
    if proposal is None:
        raise OutcomeError("That proposal does not belong to this chat", "not_found")
    if proposal["status"] == "applied":
        return await receipt(chat_id, proposal["operation_id"])
    if proposal["status"] != "pending":
        raise OutcomeError("That proposal is no longer pending", "stale")
    previous_confirmation = await receipt_for_source(chat_id, confirmation_source)
    if previous_confirmation and previous_confirmation["proposal_id"] != proposal_id:
        raise OutcomeError("That confirmation message already belongs to another proposal", "stale")
    source_guard = await _fresh_source_guard(chat_id, confirmation_source)
    timestamp = timeutil.utc_iso(now)
    if proposal["expires_at"] <= timestamp:
        await db.execute("UPDATE outcome_proposals SET status = 'expired' WHERE id = ? AND status = 'pending'", (proposal_id,))
        raise OutcomeError("That proposal expired; ask for a fresh preview", "expired")
    plan = json.loads(proposal["plan_json"])
    if plan["checkpoints"]:
        from . import outcome_checkpoints
        epoch = await outcome_checkpoints.sync_delivery_state()
        if epoch is None or epoch != plan["delivery_epoch"]:
            raise OutcomeError("Check-in delivery settings changed; a new agreement is required", "stale")
        if any(checkpoint["due_at"] <= timestamp for checkpoint in plan["checkpoints"]):
            raise OutcomeError("The proposed check-in time has passed", "expired")
    if plan["quiet"] not in (None, "unchanged") and plan["quiet"] <= timestamp:
        raise OutcomeError("The proposed quiet period has passed", "expired")
    control = {"chat_id": chat_id, "generation": proposal["generation"], "revision": proposal["control_revision"]}
    operation_id = proposal["operation_id"]
    fingerprint = await notebook_fingerprint()
    current = {}
    for key, revision in plan["expected"].items():
        row = await db.fetch_one("SELECT * FROM outcomes WHERE id = ? AND chat_id = ?", (int(key), chat_id))
        if row is None or row["revision"] != revision:
            raise OutcomeError("An outcome changed; review a fresh proposal", "stale")
        current[key] = row
    latest_control = await _control(chat_id)
    effective_quiet = latest_control["work_quiet_until"] if plan["quiet"] == "unchanged" else plan["quiet"]
    for checkpoint in plan["checkpoints"]:
        if effective_quiet is not None and checkpoint["due_at"] < effective_quiet:
            raise OutcomeError("The check-in conflicts with work quiet; choose a later time")
        key = checkpoint["target"]
        if key in current and key not in plan["values"] and current[key]["notebook_fingerprint"] != fingerprint:
            raise OutcomeError("Notebook context changed; confirm the outcome before adding a new check-in", "conflict")
    statements = [_control_guard(control), _guard(
        "EXISTS (SELECT 1 FROM outcome_proposals WHERE id = ? AND chat_id = ? AND status = 'pending' AND expires_at > ?)",
        (proposal_id, chat_id, timestamp))]
    if source_guard:
        statements.append(source_guard)
    statements.extend(_guard("EXISTS (SELECT 1 FROM outcomes WHERE id = ? AND chat_id = ? AND revision = ?)",
                             (int(key), chat_id, revision)) for key, revision in plan["expected"].items())
    for checkpoint in plan["cancellations"]:
        statements.append(_guard("EXISTS (SELECT 1 FROM outcome_checkpoints WHERE id = ? AND chat_id = ? AND revision = ? AND status = 'pending')",
                                 (checkpoint["id"], chat_id, checkpoint["revision"])))
    if plan["checkpoints"]:
        statements.append(_guard("(SELECT value FROM app_config WHERE key = 'outcome_checkpoint_delivery_epoch') = ? AND (SELECT value FROM app_config WHERE key = 'outcome_checkpoint_delivery_enabled') = 'true'",
                                 (str(plan["delivery_epoch"]),)))
    # Demote the old focus before promoting/creating a new one. The partial
    # unique index enforces the invariant even for direct database writers.
    ordered = sorted(plan["values"], key=lambda key: plan["values"][key]["state"] == "active")
    for key in ordered:
        values = plan["values"][key]
        provenance = json.loads(current[key]["provenance_json"]) if key in current else {}
        for field in plan["changed"][key]:
            provenance[field] = {"source_type": "user_report", "source_id": proposal["source_id"],
                                 "stated_at": proposal["created_at"], "last_confirmed_at": timestamp,
                                 "confirmation_source": confirmation_source, "confidence": "user_reported"}
            if plan["quotes"].get(key):
                provenance[field]["source_quote"] = plan["quotes"][key]
        if key.startswith("$"):
            columns = ("chat_id", "creation_key", *FIELDS, "provenance_json", "notebook_fingerprint", "created_at", "updated_at")
            params = (chat_id, operation_id + ":" + key, *(values[name] for name in FIELDS), _json(provenance), fingerprint, timestamp, timestamp)
            statements.append((f"INSERT INTO outcomes({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", params))
        else:
            assignments = ",".join(name + " = ?" for name in FIELDS)
            statements.append((f"UPDATE outcomes SET {assignments}, provenance_json = ?, notebook_fingerprint = ?, revision = revision + 1, updated_at = ? WHERE id = ? AND chat_id = ?",
                               (*(values[name] for name in FIELDS), _json(provenance), fingerprint, timestamp, int(key), chat_id)))
            statements.append(("UPDATE outcome_checkpoints SET status = CASE WHEN send_started_at IS NULL THEN 'cancelled' ELSE 'uncertain' END, cancelled_at = ?, updated_at = ?, revision = revision + 1 WHERE outcome_id = ? AND chat_id = ? AND status = 'pending'",
                               (timestamp, timestamp, int(key), chat_id)))
    for checkpoint in plan["cancellations"]:
        statements.append(("UPDATE outcome_checkpoints SET status = CASE WHEN send_started_at IS NULL THEN 'cancelled' ELSE 'uncertain' END, cancelled_at = ?, updated_at = ?, revision = revision + 1 WHERE id = ? AND status = 'pending'",
                           (timestamp, timestamp, checkpoint["id"])))
    for checkpoint in plan["checkpoints"]:
        selector, params = _selector(checkpoint["target"], operation_id)
        origin = operation_id + ":" + str(checkpoint["operation_index"])
        statements.append((
            ("INSERT INTO outcome_checkpoints(chat_id, outcome_id, origin_operation_id, question, due_at, expires_at, outcome_revision, agreement_source, notebook_fingerprint, delivery_epoch, generation, created_at, updated_at) "
             f"SELECT ?, id, ?, ?, ?, ?, revision, ?, ?, ?, ?, ?, ? FROM outcomes WHERE id = {selector} AND chat_id = ?"),
            (chat_id, origin, checkpoint["question"], checkpoint["due_at"], checkpoint["expires_at"], checkpoint["agreement_source"], fingerprint, plan["delivery_epoch"], proposal["generation"], timestamp, timestamp, *params, chat_id),
        ))
        statements.append(("UPDATE outcome_checkpoints SET job_key = 'outcome_checkpoint:' || id || ':1' WHERE origin_operation_id = ?", (origin,)))
    quiet = plan["quiet"]
    if quiet == "unchanged":
        statements.append(("UPDATE outcome_control SET revision = revision + 1, updated_at = ? WHERE chat_id = ?", (timestamp, chat_id)))
    else:
        statements.append(("UPDATE outcome_control SET work_quiet_until = ?, revision = revision + 1, updated_at = ? WHERE chat_id = ?", (quiet, timestamp, chat_id)))
    for index, operation in enumerate(plan["operations"]):
        statements.append(("INSERT INTO outcome_events(operation_id, proposal_id, chat_id, operation_index, delta_json, confirmation_source, result_json, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (operation_id + ":" + str(index), proposal_id, chat_id, index, _json(operation), confirmation_source,
                            _json({"status": "applied", "receipt_operation_id": operation_id}), timestamp)))
    selectors, selected_params = [], []
    for key in plan["values"]:
        selector, params = _selector(key, operation_id)
        selectors.append(selector)
        selected_params.extend(params)
    outcome_where = "outcomes.id IN (" + ",".join(selectors) + ")" if selectors else "0"
    outcome_columns = ("id", "chat_id", *FIELDS, "revision", "created_at", "updated_at", "provenance_json", "notebook_fingerprint")
    cp_columns = ("id", "outcome_id", "question", "due_at", "expires_at", "status", "outcome_revision", "revision", "agreement_source")
    checkpoint_where = "outcome_checkpoints.origin_operation_id LIKE ?"
    checkpoint_params = [operation_id + ":%"]
    changed_existing = [int(key) for key in plan["values"] if not key.startswith("$")]
    if changed_existing:
        checkpoint_where += " OR (outcome_checkpoints.outcome_id IN (" + ",".join("?" for _ in changed_existing) + ") AND outcome_checkpoints.cancelled_at = ?)"
        checkpoint_params.extend([*changed_existing, timestamp])
    if plan["cancellations"]:
        checkpoint_where += " OR outcome_checkpoints.id IN (" + ",".join("?" for _ in plan["cancellations"]) + ")"
        checkpoint_params.extend(item["id"] for item in plan["cancellations"])
    receipt_sql = (
        "UPDATE outcome_proposals SET status = 'applied', confirmation_source = ?, applied_at = ?, receipt_json = "
        "json_object('operation_id',?,'proposal_id',?,'status','applied','applied_at',?,'source_id',?,'confirmation_source',?,"
        f"'outcomes',json((SELECT json_group_array({_row_json('outcomes', outcome_columns)}) FROM outcomes WHERE {outcome_where})),"
        f"'checkpoints',json((SELECT json_group_array({_row_json('outcome_checkpoints', cp_columns)}) FROM outcome_checkpoints WHERE {checkpoint_where})),"
        "'work_quiet_until',(SELECT work_quiet_until FROM outcome_control WHERE chat_id = ?)) WHERE id = ? AND chat_id = ?"
    )
    statements.append((receipt_sql, (confirmation_source, timestamp, operation_id, proposal_id, timestamp, proposal["source_id"], confirmation_source,
                                    *selected_params, *checkpoint_params, chat_id, proposal_id, chat_id)))
    try:
        await db.execute_batch(statements)
    except Exception as exc:
        # A lost acknowledgement can follow a successful remote commit. Never
        # blindly repeat creation; the immutable receipt resolves that boundary.
        saved = await reconcile(chat_id, operation_id)
        if saved:
            return saved
        await _fresh_source_guard(chat_id, confirmation_source)
        latest = await _control(chat_id)
        if latest["revision"] != control["revision"] or latest["generation"] != control["generation"]:
            raise OutcomeError("Priorities changed; review a fresh proposal", "stale") from exc
        for key, revision in plan["expected"].items():
            latest_row = await db.fetch_one("SELECT revision FROM outcomes WHERE id = ? AND chat_id = ?", (int(key), chat_id))
            if latest_row is None or latest_row["revision"] != revision:
                raise OutcomeError("An outcome changed; review a fresh proposal", "stale") from exc
        raise
    saved = await receipt(chat_id, operation_id)
    if not saved:
        raise OutcomeError("The write result is uncertain; reconcile this operation before retrying", "uncertain")
    return saved


async def reject(chat_id, proposal_id, source_id=None) -> bool:
    chat_id = _chat(chat_id)
    rows = await db.execute_returning(
        "UPDATE outcome_proposals SET status = 'rejected', confirmation_source = ? WHERE id = ? AND chat_id = ? AND status = 'pending' RETURNING id",
        (_source(source_id) if source_id is not None else None, _text(proposal_id, "proposal_id", 100), chat_id),
    )
    return bool(rows)


async def invalidate_pending(chat_id) -> None:
    await db.execute_batch([
        ("UPDATE outcome_proposals SET status = 'superseded' WHERE chat_id = ? AND status = 'pending'", (_chat(chat_id),)),
        ("UPDATE outcome_control SET revision = revision + 1, updated_at = ? WHERE chat_id = ?", (timeutil.utc_iso(), _chat(chat_id))),
    ])


async def disable(chat_id=None) -> None:
    """Rollback epoch: preserve data but retire prior previews and check-ins."""
    timestamp = timeutil.utc_iso()
    where = "" if chat_id is None else " AND chat_id = ?"
    params = () if chat_id is None else (_chat(chat_id),)
    await db.execute_batch([
        ("UPDATE outcome_control SET generation = generation + 1, revision = revision + 1, updated_at = ? WHERE 1 = 1" + where, (timestamp, *params)),
        ("UPDATE outcome_proposals SET status = 'superseded' WHERE status = 'pending'" + where, params),
        ("UPDATE outcome_checkpoints SET status = CASE WHEN send_started_at IS NULL THEN 'cancelled' ELSE 'uncertain' END, cancelled_at = ?, updated_at = ?, revision = revision + 1 WHERE status = 'pending'" + where,
         (timestamp, timestamp, *params)),
    ])


async def direct_command(chat_id, source_id, operations, source_text, now=None) -> Receipt:
    proposal = await propose(chat_id, source_id, operations, source_text, now=now)
    return await confirm(chat_id, proposal["id"], source_id, now=now)
