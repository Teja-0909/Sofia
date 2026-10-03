# Accountability and persistent-state alignment

This change makes Sofia a warm, witty accountability companion for the user's current priorities. It is not a fixed study/coding coach. Rest, changed plans, an important personal commitment, and a useful conversation can all take precedence over an old sprint.

## What is policy and what is evidence

`system_prompt.txt` defines the main role, dynamic prioritization, honest capabilities, supportive pushback, user control and interruption standard. `orchestrator_context.CONTEXT_POLICY` supplies the same authority rules for every assembled turn. The time block is a clock, not a prescribed daily routine.

Persistent prose now travels in a separate JSON reference message, not inside the system instruction string. Each section identifies its source and available dates. The retrieval time is not a user-confirmation time. Task descriptions, notebook edits, recalled conversation, generated reflections and window titles cannot grant permissions or override the latest direct request. The current-commitment section gets space before optional older history; limits and truncation are explicit.

The state paths inspected are:

- `conversation_log`: direct user messages and command activity; read for recent conversation, summaries and background-contact guards. State-changing commands record the actual request before mutation/model work so an in-flight nudge can be invalidated
- `app_config.active_focus_goal` / `active_focus_started_at`: explicit `/focus` controls; read as a dated, revisable plan, with no claim of continuous work or automatic priority
- `tasks`: application-confirmed reminders/completion/cancellation/snoozing; both pending and missed-but-not-cancelled records remain visible as unresolved historical records. `due_time` is a reminder time, not a proven external deadline
- `app_config.memory_md_content` and `relationship_memory`: editable notebook and saved claims; existing manual-edit reconciliation, compare-and-swap behavior and suppression records remain intact
- `conversation_summaries`, `daily_diary`, `diary_chapters`: generated, dated paraphrases/reflections, not independent confirmation. Curator and summary inputs preserve speaker and timestamp. Diary input/output also applies the existing suppression filter
- `consciousness_state`, mood configuration, `inner_thoughts`, `dreams`: compatibility state and optional presentation hints; generated thoughts are hypotheses and creative dreams are fiction. They cannot autonomously schedule messages
- `delivery_claims` and `job_runs`: durable delivery receipts/leases, reused for shared background cooldown and unanswered-contact checks
- `proactive_messages`: existing saved message queue, still delivered literally through its reliable scheduled path

No private production database or notebook was inspected or rewritten for this change. Existing persisted facts are not mass-edited. Curation still appends claims; an explicit plan change is captured as dated content, not a new canonical priority-management system.

## Proactivity behavior

Background review must find a meaningful, current benefit: an agreed checkpoint, concrete blocker, actual deadline risk, needed decision or important new result. Silence, an app change, time of day, a restart or a generated desire to talk is insufficient. Otherwise the result is exactly `PASS`, including when tracing is enabled.

Unsolicited routes share persistent pause/rest checks, a one-hour cooldown after acknowledged scheduled contact, a recent-conversation quiet window, and a stop after an unanswered unsolicited message. They recheck pause/rest and newer user activity immediately before sending. Different routes share a database lease for the same latest user-message context rather than independently sending at once, including across an hour boundary. Random just-because outreach is removed. The background generator no longer invokes the incoming-user wake path.

Explicit progress acknowledgements such as `/win` and `/focus done` use a foreground acknowledgement path, so they do not inherit the unsolicited-contact `PASS` rule. An explicitly started `/watch` session remains a separate bounded observation flow with no tools or wake side effects; active-session, pause/rest and session-identity guards protect its send boundary.

- `/pause on` continues to pause reminders, background messages and desktop activity; ordinary replies still work
- `/sleep` suppresses unsolicited contact until the next incoming chat message; it does not cancel saved reminders
- `/focus <new goal>` and `/focus clear` update the saved plan explicitly
- `/cancel`, `/snooze`, `/done`, `/correct` and `/forget` keep their existing permission and persistence boundaries

Deterministic reminders keep their saved wording and delivery cadence. This change does not use a model to rewrite or postpone a requested reminder.

## Six representative acceptance scenarios

These are target behaviors and offline contract scenarios, not claims that a live model produced these exact replies.

1. **Changed priority:** An old study goal exists, but the user now needs interview preparation. Help with the interview and offer one practical first step. Do not force study or claim the saved sprint was changed without a confirmed control action
2. **Stale sprint:** A sprint was saved several days ago. Treat it as possibly superseded; ask whether it still matters if that affects the recommendation. Do not claim the user worked on it continuously
3. **Real deadline:** The user reports an application closes at 17:00. Prioritize the mandatory submission steps and explain the tradeoff against optional polishing. An unrelated overdue reminder does not automatically outrank it
4. **Chosen rest:** The user says they are taking a break. Respect that without guilt or another productivity task. Explicit pause/rest controls must also prevent in-flight unsolicited delivery
5. **No reply:** A useful check-in was sent and has not been answered. Do not send another unsolicited nudge or infer avoidance. Saved requested reminders remain a distinct path
6. **Uncertain PC:** The sample is stale, paused or unavailable. Say current activity is unknown; never infer procrastination, closed apps, sleep or physical absence from it

## Verification and remaining limits

All automated checks use temporary databases, mocked model/Telegram/desktop boundaries and offline fixtures. They cover prompt/message authority contracts, state persistence across connection restarts, suppression, command activity order, background cooldown/quiet behavior, concurrent delivery, rest/pause/new-message races and existing reminder/notebook/security regressions. These tests do not prove a probabilistic model will always make the desired judgment. A small supervised conversation check after an independently approved rollout is still useful.

Remaining architectural limits are explicit:

- There is no dedicated schema for ranked priorities, next steps, blockers, progress evidence, checkpoint agreement or real deadlines. The model must reason from the latest dated conversation and existing records; natural-language rest/quiet preferences outside explicit controls remain model-interpreted
- Reminder delivery time and real-world deadline are still different concepts sharing the existing task record. This change avoids confusing them; it does not add deadline tracking
- Memory supersession is contextual, not automatic deletion/replacement of all contradictory historical rows. Existing explicit correction and manual notebook controls remain the reliable editing routes
- Already queued `proactive_messages` have no origin field distinguishing old generated outreach from explicitly saved messages. They are preserved to avoid deleting requested schedules. This change stops new thought-generated scheduling; it cannot safely identify or erase old such rows
- Context limits reserve space for conversation/tools but use approximate character accounting. They are not an exact total provider-token budget
- Suppression uses normalized text containment, not semantic erasure of every possible paraphrase. Historical audit rows remain
- Telegram can accept a send before an acknowledgement is lost. Existing delivery leases bound retries but do not promise exactly-once delivery across that failure boundary

## Rollout

Review the complete draft change together. This is a server/application update; no sidecar protocol change, dependency, credential, paid model test or schema migration is required. Existing config keys and mood identifiers remain compatible. No merge or deployment was performed as part of this work. Deploy only after separate approval, preserving private configuration, and review the six scenarios against the chosen live model before relying on its judgment for important commitments.
