"""Verified Telegram-facing outcome proposals, receipts and explicit controls."""
import datetime as dt
import json
import logging
import re

from . import config, db, memory, outcome_intents, timeutil

logger = logging.getLogger(__name__)
FAILURE = "I couldn't verify that priority change. Check /outcomes before retrying."
DISABLED = "Priority tracking is not enabled in this deployment. Your existing reminders and focus controls still work."

OUTCOME_POLICY = """Confirmed outcome tracking:
- The confirmed outcome snapshot is current application state, above older prose,
  but the user's latest direct choice takes precedence for advice even while a
  save awaits confirmation. One chosen focus and up to three next items are a
  view, not a hidden importance score. Other outcomes remain retrievable.
- Distinguish outcomes (work), checkpoints (agreed conversations), and timers or
  reminders (notifications). Timer delivery, elapsed time, silence, a diary or a
  desktop title never proves work complete. Progress is the user's report.
- Real hard deadlines and target dates are labelled separately from notification
  times. Unknown facts remain unknown. Passed deadlines need reassessment, not
  permanent urgency, invented failure or silently moved dates. Recommend an
  actionable next step using stated importance, blockers, actual deadlines and
  the user's stated time/energy; explain tradeoffs. Do not silently reorder.
- A sprint is a temporary activity, never a second canonical current priority.
  Do not restore old study goals from history or from an unlinked sprint.
- Never claim an outcome/checkpoint was saved, completed, parked, changed or
  scheduled from generated prose. Only caller-rendered application receipts
  verify writes. Pending proposals are visibly provisional. Do not output action
  tags. /outcomes inspects state; /outcome controls are the explicit fallback.
- No automatic planning messages, recurring checkpoints or escalating follow-ups.
  Check-ins need explicit one-shot agreement. Respect rest/quiet/cooldown and
  unanswered contact. New user activity does not itself authorize a check-in.
- If notebook_changed is true, mention a possible conflict only when relevant
  and ask which version should stand. Do not use disputed state for nudges. Never
  reconstruct suppressed/deleted facts from outcomes, events or old messages.
"""


def enabled() -> bool:
    return bool(config.ENABLE_OUTCOMES)


def source_identity(update) -> str | None:
    message = getattr(update, "message", None)
    chat = getattr(getattr(update, "effective_chat", None), "id", None)
    ident = getattr(message, "message_id", None)
    if isinstance(chat, int) and isinstance(ident, int):
        return f"telegram:{chat}:{ident}"
    return None


async def safe_snapshot(chat_id: int) -> dict:
    from . import memory_file, outcome_store
    # Apply the existing editable-notebook reconciliation before retrieval, as
    # ordinary conversation already does. Never reconstruct it from outcomes.
    await memory_file.get_memory_md()
    snapshot = await outcome_store.snapshot(chat_id)
    safe = dict(snapshot)
    rows = []
    withheld = 0
    for row in snapshot.get("outcomes", []):
        # Suppression is line/containment based today. Filter the full row; do not
        # keep an ID with a partially reconstructed suppressed fact.
        encoded = json.dumps(row, ensure_ascii=False)
        if await memory.filter_suppressed_text(encoded) != encoded:
            withheld += 1
            continue
        rows.append(row)
    safe["outcomes"] = rows
    terminal = await db.fetch_all(
        "SELECT id, title, state, updated_at, revision FROM outcomes WHERE chat_id = ? "
        "AND state IN ('completed','dropped') ORDER BY updated_at DESC, id DESC LIMIT 20", (str(chat_id),),
    )
    safe["terminal"] = [row for row in terminal if await memory.filter_suppressed_text(
        json.dumps(row, ensure_ascii=False)) == json.dumps(row, ensure_ascii=False)]
    ids = {row["id"] for row in rows}
    focus = snapshot.get("focus")
    safe["focus"] = focus if focus and focus.get("id") in ids else None
    safe["next"] = [r for r in snapshot.get("next", []) if r.get("id") in ids]
    safe["checkpoints"] = [cp for cp in snapshot.get("checkpoints", [])
                           if cp.get("outcome_id") in ids
                           and await memory.filter_suppressed_text(json.dumps(cp, ensure_ascii=False)) == json.dumps(cp, ensure_ascii=False)]
    # Pending proposal text and checkpoint questions can carry the same deleted
    # evidence as an outcome; never retain them through a copied snapshot.
    pending = snapshot.get("pending")
    safe["pending"] = pending if (pending and await memory.filter_suppressed_text(
        json.dumps(pending, ensure_ascii=False)) == json.dumps(pending, ensure_ascii=False)) else None
    safe["withheld_count"] = withheld
    return safe


def _oid(row: dict) -> str:
    value = str(row.get("id", row.get("outcome_id", "?")))
    return value if value.startswith("O") else "O" + value


def _describe(row: dict, *, details: bool = False) -> str:
    line = f"{_oid(row)}: {row['title']} [{row['state']}]"
    if row.get("deadline"):
        line += f"; {row.get('deadline_kind') or 'deadline'}: {row['deadline']}"
        if row.get("deadline_timezone"):
            line += f" ({row['deadline_timezone']})"
        try:
            due = str(row["deadline"])
            passed = (dt.date.fromisoformat(due) < timeutil.now_local().date() if len(due) == 10
                      else dt.datetime.fromisoformat(due.replace("Z", "+00:00")) < timeutil.utc_now())
            if passed:
                line += "; passed, needs reassessment"
        except (ValueError, TypeError):
            pass
    if row.get("next_step"):
        line += f"\n  Next: {row['next_step']}"
    if row.get("blocker"):
        line += f"\n  Blocked: {row['blocker']}"
    if row.get("progress"):
        line += f"\n  Reported progress: {row['progress']}"
    if details:
        for key, label in (("importance", "Importance"), ("reason", "Why"), ("effort", "Effort"), ("queue_position", "Queue position")):
            if row.get(key):
                line += f"\n  {label}: {row[key]}"
        line += f"\n  Revision {row['revision']}; updated {row.get('updated_at', 'unknown')}"
    if row.get("notebook_changed"):
        line += "\n  Notebook changed since confirmation; check which version applies before another check-in."
    return line


async def board(chat_id: int, *, all_items: bool = False) -> str:
    snapshot = await safe_snapshot(chat_id)
    rows = snapshot.get("outcomes", [])
    if all_items:
        rows = rows + snapshot.get("terminal", [])
        shown = rows[:40]
        lines = ["Tracked outcomes (up to 40):"] + [_describe(row) for row in shown]
        if len(rows) > 40:
            lines.append("More are saved; use /outcome O<id> to inspect an individual outcome.")
    else:
        focus = snapshot.get("focus")
        lines = ["Current focus: " + (_describe(focus) if focus else "none selected")]
        upcoming = snapshot.get("next", [])[:3]
        if upcoming:
            lines += ["Next:"] + [_describe(row) for row in upcoming]
        others = max(0, len(rows) - len(upcoming) - int(bool(focus)))
        if others:
            lines.append(f"{others} other outcome(s) saved; /outcomes all shows them.")
    if not rows:
        lines.append("No confirmed outcomes yet. Tell me what you want to track, or use /outcome add <title>.")
    if snapshot.get("withheld_count"):
        lines.append("Some records are withheld by your memory-suppression controls.")
    quiet = snapshot.get("control", {}).get("work_quiet_until")
    if quiet:
        lines.append(f"Work check-ins quiet until {quiet}.")
    return "\n".join(lines)


async def context_block() -> str:
    if not enabled():
        return ""
    try:
        snapshot = await safe_snapshot(config.ALLOWED_USER_ID)
        rows = snapshot.get("outcomes", [])
        # Show chosen state first, then bounded extra records. No hidden ranking.
        selected = snapshot.get("focus")
        first = ([selected] if selected else []) + snapshot.get("next", [])
        first_ids = {r["id"] for r in first}
        snapshot["outcomes"] = first + [r for r in rows if r["id"] not in first_ids][:16]
        snapshot["truncated"] = len(snapshot["outcomes"]) < len(rows)
        return json.dumps({"kind": "confirmed_outcome_state", "retrieved_at": timeutil.utc_iso(),
                           "latest_user_choice_overrides_for_advice": True, "data": snapshot}, ensure_ascii=False)
    except Exception:
        return "Confirmed outcome state unavailable. Do not infer saved priorities or success."


async def preview_text(proposal: dict) -> str:
    preview = proposal.get("preview", "")
    if not isinstance(preview, str):
        preview = json.dumps(preview, ensure_ascii=False)
    lines = ["Proposed (not saved):", preview]
    warnings = proposal.get("warnings", [])
    lines.extend(str(w) for w in warnings)
    lines.append(f"Reply Save or Adjust, or /outcome save {proposal['id']}. This preview expires in 15 minutes.")
    return "\n".join(lines)


async def receipt_text(receipt: dict) -> str:
    if receipt.get("status") != "applied":
        return FAILURE
    lines = ["Saved."]
    for row in receipt.get("outcomes", []):
        encoded = json.dumps(row, ensure_ascii=False)
        if await memory.filter_suppressed_text(encoded) == encoded:
            lines.append(_describe(row))
        else:
            lines.append("An outcome update is recorded; its text is withheld by your suppression controls.")
    for row in receipt.get("checkpoints", []):
        if row.get("status") == "pending":
            lines.append(f"Check-in C{row['id']} around {row['due_at']} (UTC), subject to rest and contact limits.")
        elif row.get("status") == "cancelled":
            lines.append(f"Check-in C{row['id']} cancelled.")
    if receipt.get("work_quiet_until"):
        lines.append(f"Work check-ins quiet until {receipt['work_quiet_until']}.")
    return "\n".join(lines)


async def _checkpoint_conflict(operations: list[dict], chat_id: int) -> str | None:
    from . import consciousness, outcome_checkpoints, triggers
    checkpoints = [op for op in operations if op.get("op") == "checkpoint"]
    if not checkpoints:
        return None
    if not config.ENABLE_OUTCOME_CHECKPOINTS:
        return "Checkpoint delivery isn't enabled yet. I can track the outcome without a check-in; remove the check-in from this request to save it."
    await outcome_checkpoints.sync_delivery_state()
    if await db.get_config("proactivity_paused", "false") == "true" or await consciousness.is_sleeping_async():
        return "Work check-ins are paused or resting. Resume the relevant control before agreeing to a check-in."
    from . import outcome_store
    snapshot = await outcome_store.snapshot(chat_id)
    quiet_until = snapshot.get("control", {}).get("work_quiet_until")
    for op in operations:
        if op.get("op") == "quiet":
            quiet_until = op.get("until")
    now = timeutil.utc_now()
    latest = await triggers._last_delivery(triggers.BACKGROUND_KINDS + ("reminder_send", "proactive_send"))
    earliest = now + dt.timedelta(minutes=30)  # this conversation itself starts the quiet window
    if latest and latest.get("sent_at"):
        earliest = max(earliest, triggers._parse_stored_timestamp(latest["sent_at"]) + dt.timedelta(hours=1))
    for op in checkpoints:
        try:
            due = dt.datetime.fromisoformat(op["due_at"].replace("Z", "+00:00"))
            if due.tzinfo is None:
                raise ValueError("aware time required")
            local = due.astimezone(timeutil.tz())
            start, end = config.QUIET_START_HOUR, config.QUIET_END_HOUR
            quiet_hour = start <= local.hour < end if start < end else local.hour >= start or local.hour < end
            work_quiet = quiet_until and due < dt.datetime.fromisoformat(quiet_until.replace("Z", "+00:00"))
            if due < earliest or quiet_hour or work_quiet:
                return (f"That check-in conflicts with a quiet window or contact cooldown. "
                        f"Choose a time after {earliest.astimezone(timeutil.tz()).isoformat()} outside configured quiet hours.")
        except (ValueError, TypeError, KeyError):
            return "What timezone-aware date and time should that check-in use?"
    return None


async def _history_for_interpretation(text: str) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT role, content, timestamp FROM conversation_log ORDER BY id DESC LIMIT 9"
    )
    if rows and rows[0]["role"] == "user" and rows[0]["content"] == text:
        rows = rows[1:]
    result = []
    for row in reversed(rows[:8]):
        result.append({**row, "content": (await memory.filter_suppressed_text(row["content"]))[:1500]})
    return result


async def _ambiguous_done(text: str, chat_id: int) -> str | None:
    if text.casefold().strip(" .!?") not in {"done", "finished", "completed", "did it"}:
        return None
    from . import outcome_store, tasks
    snapshot = await outcome_store.snapshot(chat_id)
    open_outcomes = snapshot.get("outcomes", [])
    pending_tasks = await tasks.list_pending()
    # Include recent timer alerts, not just pending timers. A timer alert is a
    # plausible conversational referent but never a completed work outcome.
    recent_timers = await db.fetch_all(
        "SELECT id FROM tasks WHERE kind = 'timer' AND cancelled_at IS NULL "
        "AND julianday(COALESCE(last_reminded_at, completed_at, created_at)) >= julianday(?) - 1 LIMIT 1",
        (timeutil.utc_iso(),),
    )
    if open_outcomes and (len(open_outcomes) > 1 or pending_tasks or recent_timers):
        return "Done with which work outcome, or just the timer/reminder? Use an O-prefixed outcome ID; /done <number> keeps its reminder meaning."
    if open_outcomes:
        row = open_outcomes[0]
        return f"Is {_oid(row)} complete, or just its current step? Tell me the scope before I change it."
    if pending_tasks and (len(pending_tasks) > 1 or recent_timers):
        return "Which task did you finish? /tasks shows the IDs; a finished timer doesn't mark the work done."
    return None


async def handle_text(update, text: str) -> str | None:
    """Return a caller-owned response or None for normal read-only generation."""
    if not enabled():
        return None
    from . import outcome_store
    chat_id = update.effective_chat.id
    if getattr(update.message, "forward_origin", None) or getattr(update.message, "forward_date", None):
        return None
    source = source_identity(update)
    if not source:
        return FAILURE  # real Telegram messages always carry an identity
    try:
        from . import memory_file
        await memory_file.get_memory_md()
        existing_receipt = await outcome_store.receipt_for_source(chat_id, source)
        if existing_receipt:
            return await receipt_text(existing_receipt)
        if not await outcome_store.is_current_source(chat_id, source):
            return "That older message is superseded. Review the current proposal or send a new request."
        pending = await outcome_store.pending(chat_id)
        if pending:
            encoded = json.dumps(pending, ensure_ascii=False)
            if await memory.filter_suppressed_text(encoded) != encoded:
                await outcome_store.invalidate_pending(chat_id)
                pending = None
        approval = outcome_intents.confirmation(text)
        if pending and approval:
            replied = getattr(update.message, "reply_to_message", None)
            if replied is not None:
                reply_text = getattr(replied, "text", "") or ""
                match = re.search(r"/outcome save (P[a-f0-9]{24})\b", reply_text)
                sender = getattr(replied, "from_user", None)
                # A targeted reply cannot silently approve some other, newer
                # preview. If we cannot identify this preview, ask for its ID.
                if (not match or match.group(1) != str(pending["id"])
                        or (sender is not None and not getattr(sender, "is_bot", False))):
                    return "That reply doesn't identify the current preview. Review the latest proposal and use its /outcome save ID."
            if approval == "reject":
                await outcome_store.reject(chat_id, pending["id"], source)
                return "Discarded that proposal."
            return await receipt_text(await outcome_store.confirm(chat_id, pending["id"], source))
        if approval == "save":
            return "There isn't a current proposal to save. Tell me the change you'd like."
        ambiguous = await _ambiguous_done(text, chat_id)
        if ambiguous:
            await outcome_store.invalidate_pending(chat_id)
            return ambiguous
        if text.casefold().strip(" .!?") in {"adjust", "change that"} and pending:
            return "What should change in that proposal?"
        snapshot = await safe_snapshot(chat_id)
        snapshot["outcomes"] = snapshot.get("outcomes", [])[:40]
        activity = await db.fetch_one("SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
        try:
            interpretation = await outcome_intents.interpret(
                text, snapshot, pending=pending, history=await _history_for_interpretation(text),
            )
        except Exception as exc:
            logger.warning("Outcome interpretation unavailable (%s)", type(exc).__name__)
            await outcome_store.invalidate_pending(chat_id)
            if pending or re.search(r"\b(?:track|priority|priorities|outcome|goal|checkpoint|check.in|next step)\b", text, re.IGNORECASE):
                return "I couldn't interpret a safe priority change. Nothing was applied; /outcome help shows explicit controls."
            return None
        current_activity = await db.fetch_one("SELECT id FROM conversation_log WHERE role = 'user' ORDER BY id DESC LIMIT 1")
        if activity != current_activity:
            return "A newer message arrived while I interpreted that change. Please use the latest conversation or an explicit outcome control."
        intent = interpretation["intent"]
        if intent == "propose":
            operations = interpretation["operations"]
            conflict = await _checkpoint_conflict(operations, chat_id)
            if conflict:
                await outcome_store.invalidate_pending(chat_id)
                return conflict
            evidence = interpretation["source_quotes"][0][:500]
            span_start = text.index(evidence)
            for op in operations:
                op["source_quote"] = evidence
                op["source_span"] = [span_start, span_start + len(evidence)]
                if op["op"] == "checkpoint":
                    op["agreement_source"] = source
            proposal = await outcome_store.propose(chat_id, source, operations, text)
            return await preview_text(proposal)
        # A generic later 'yes' cannot approve an earlier unrelated preview.
        await outcome_store.invalidate_pending(chat_id)
        if intent == "show":
            return await board(chat_id)
        if intent == "clarify":
            from .action_grounding import guard_generated_reply
            question = interpretation["question"].strip()
            return guard_generated_reply(question, user_text=text) if question else "Which outcome and change do you mean?"
        return None
    except outcome_store.OutcomeError as exc:
        return f"No new change confirmed: {exc}"
    except Exception as exc:
        logger.warning("Outcome conversation unavailable (%s)", type(exc).__name__)
        return FAILURE


COMMAND_HELP = (
    "/outcomes [all] shows saved priorities. /outcome O1 shows details.\n"
    "/outcome add <title>\n/outcome focus|park|resume|done|drop O1\n"
    "/outcome next|progress|block O1 <text>\n"
    "/outcome importance O1 low|medium|high [reason]\n/outcome order O1 <position>\n"
    "/outcome deadline O1 hard|target YYYY-MM-DD\n"
    "/outcome quiet <timezone-aware ISO time>|off\n"
    "/outcome save <proposal ID> or /outcome discard\n"
    "For a one-shot check-in, ask in chat with its date and time. Changing an outcome invalidates its old work check-ins; alarms are separate."
)


async def command(update, args: list[str]) -> str:
    if (getattr(update.message, "forward_origin", None)
            or getattr(update.message, "forward_date", None)):
        return "Forwarded text can't change your outcomes. Send the command directly if you want that change."
    if not enabled():
        return DISABLED
    from . import outcome_store
    chat_id = update.effective_chat.id
    source = source_identity(update)
    if not source:
        return FAILURE
    try:
        from . import memory_file
        await memory_file.get_memory_md()
        existing = await outcome_store.receipt_for_source(chat_id, source)
        if existing:
            return await receipt_text(existing)
        if not await outcome_store.is_current_source(chat_id, source):
            return "That older command is superseded. Send a new command for the current state."
        if not args:
            return await board(chat_id)
        if args == ["all"]:
            return await board(chat_id, all_items=True)
        if len(args) == 1 and re.fullmatch(r"O\d+", args[0], re.IGNORECASE):
            row = await outcome_store.get(args[0].upper(), chat_id)
            if not row:
                return "That outcome wasn't found."
            encoded = json.dumps(row, ensure_ascii=False)
            if await memory.filter_suppressed_text(encoded) != encoded:
                return "That record is withheld by memory-suppression controls."
            return _describe(row, details=True)
        sub = args[0].casefold()
        if sub in {"discard", "save"}:
            pending = await outcome_store.pending(chat_id)
            if not pending:
                return "There isn't a current proposal."
            if sub == "discard":
                await outcome_store.reject(chat_id, pending["id"], source)
                return "Discarded that proposal."
            if len(args) != 2 or str(args[1]) != str(pending["id"]):
                return "Use the proposal ID shown in the current preview."
            return await receipt_text(await outcome_store.confirm(chat_id, pending["id"], source))
        operations = []
        if sub == "add" and len(args) >= 2:
            operations = [{"op": "create", "fields": {"title": " ".join(args[1:]), "state": "queued"}}]
        elif sub == "quiet" and len(args) == 2:
            operations = [{"op": "quiet", "until": None if args[1].casefold() == "off" else args[1]}]
        elif len(args) >= 2 and re.fullmatch(r"O\d+", args[1], re.IGNORECASE):
            row = await outcome_store.get(args[1].upper(), chat_id)
            if not row:
                return "That outcome wasn't found."
            operation = {"op": "update", "outcome_id": row["id"], "expected_revision": row["revision"]}
            states = {"park": "paused", "resume": "queued", "done": "completed", "drop": "dropped"}
            if sub == "focus" and len(args) == 2:
                operation["op"] = "select"
            elif sub in states and len(args) == 2:
                operation["fields"] = {"state": states[sub]}
            elif sub in {"next", "progress", "block"} and len(args) >= 3:
                field = {"next": "next_step", "progress": "progress", "block": "blocker"}[sub]
                operation["fields"] = {field: " ".join(args[2:])}
                if sub == "block":
                    operation["fields"]["state"] = "blocked"
            elif sub == "order" and len(args) == 3:
                operation["fields"] = {"queue_position": int(args[2])}
            elif sub == "importance" and len(args) >= 3:
                operation["fields"] = {"importance": args[2]}
                if len(args) > 3:
                    operation["fields"]["reason"] = " ".join(args[3:])
            elif sub == "deadline" and len(args) == 4:
                # Date-only is deliberately preserved. Timed deadlines are proposed
                # through conversation so the timezone is visible before save.
                dt.date.fromisoformat(args[3])
                operation["fields"] = {"deadline_kind": args[2], "deadline": args[3]}
            else:
                return COMMAND_HELP
            operations = [operation]
        else:
            return COMMAND_HELP
        proposal = await outcome_store.propose(chat_id, source, operations, update.message.text or "")
        # Unexpected linked checkpoint effects are always previewed first.
        if proposal.get("warnings"):
            return await preview_text(proposal)
        return await receipt_text(await outcome_store.confirm(chat_id, proposal["id"], source))
    except (outcome_store.OutcomeError, ValueError) as exc:
        return f"No new change confirmed: {exc}"
    except Exception as exc:
        logger.warning("Outcome command unavailable (%s)", type(exc).__name__)
        return FAILURE


async def invalidate_for_other_action(chat_id: int) -> None:
    """A later generic yes may not approve a preview before an unrelated action."""
    if enabled():
        from . import outcome_store
        await outcome_store.invalidate_pending(chat_id)


async def duplicate_response(update) -> str:
    """Reconcile known results without rebinding an old message as fresh consent."""
    from . import outcome_store
    chat_id, source = update.effective_chat.id, source_identity(update)
    try:
        from . import memory_file
        await memory_file.get_memory_md()
        receipt = await outcome_store.receipt_for_source(chat_id, source)
        if receipt:
            return await receipt_text(receipt)
        proposal = await outcome_store.pending(chat_id)
        if proposal and proposal.get("source_id") == source:
            encoded = json.dumps(proposal, ensure_ascii=False)
            if await memory.filter_suppressed_text(encoded) == encoded:
                return await preview_text(proposal)
        return "That message was already received. Check /outcomes or /tasks for saved state; send a new message if you want a new change."
    except Exception:
        return FAILURE
