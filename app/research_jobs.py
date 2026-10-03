"""Bounded research workers, separate from conversational handler/tool locks.

The scheduler never awaits research/provider/Telegram work. Its two task slots
are shared by execution and result delivery. Engine callbacks and the poller
both fence leases, revisions, cancellation and global pause. Research stays
read-only; only this runner can deliver its persisted results through bot_core.
"""

import asyncio
import contextlib
import logging

from . import research_store as store

logger = logging.getLogger(__name__)


class ResearchInterrupted(Exception):
    """The worker no longer holds a current, unpaused execution lease."""


class ResearchRunner:
    def __init__(self, bot=None, engine=None, *, poll_seconds=1.0, lease_seconds=30.0,
                 timeout_seconds=180.0, send_timeout_seconds=30.0):
        self.bot = bot
        self.engine = engine
        self.poll_seconds = max(0.01, min(5.0, float(poll_seconds)))
        self.lease_seconds = max(1.0, min(60.0, float(lease_seconds)))
        self.timeout_seconds = max(0.01, min(180.0, float(timeout_seconds)))
        self.send_timeout_seconds = max(0.01, min(30.0, float(send_timeout_seconds)))
        self._loop_task = None
        self._workers = {}
        self._poll_lock = asyncio.Lock()
        self._stopping = False

    @property
    def running(self):
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def active_count(self):
        return sum(not task.done() for task in self._workers)

    async def start(self):
        if self.running or not store.enabled():
            return
        await store.validate()
        self._stopping = False
        await store.recover_expired()
        self._loop_task = asyncio.create_task(self._loop(), name="research-poller")

    async def stop(self):
        self._stopping = True
        loop = self._loop_task
        self._loop_task = None
        if loop:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
        workers = list(self._workers)
        for task in workers:
            task.cancel()
        if workers:
            # A misbehaving injected/provider coroutine cannot hang shutdown.
            done, pending = await asyncio.wait(workers, timeout=5)
            for task in done:
                self._consume(task)
            for task in pending:
                job, kind = self._workers[task]
                if kind == "research":
                    try:
                        await store.release(job)
                    except Exception:
                        # A storage outage cannot prevent Telegram/application
                        # shutdown. The durable lease will expire on recovery.
                        logger.error("Research shutdown lease release failed")
                logger.warning("Research task ignored shutdown cancellation")
        self._workers.clear()

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                # Provider exceptions may include queries/keys; keep logs generic.
                logger.error("Background research task failed outside its guarded boundary")

    async def _loop(self):
        while not self._stopping:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Research queue poll failed; will retry on the next tick")
            await asyncio.sleep(self.poll_seconds)

    async def poll(self):
        """Claim bounded work and return without awaiting any provider or sender."""
        async with self._poll_lock:
            if self._stopping:
                return
            for task in list(self._workers):
                if task.done():
                    self._consume(task)
                    self._workers.pop(task)
            await store.recover_expired()
            available = store.enabled() and not await store.paused()
            for task, (job, kind) in list(self._workers.items()):
                if kind == "research" and (not available or not await store.heartbeat(job, self.lease_seconds)):
                    task.cancel()
            if not available:
                return
            # Research task slots and delivery task slots are shared: at most 2.
            while len(self._workers) < store.MAX_WORKERS:
                bot = self.bot
                if bot is None:
                    from . import bot_core
                    bot = bot_core.get_bot()
                if bot is not None:
                    delivery = await store.claim_delivery(self.send_timeout_seconds + 10)
                    if delivery:
                        task = asyncio.create_task(self._deliver(delivery, bot), name=f"research-result-{delivery['job_id']}")
                        self._workers[task] = (delivery, "delivery")
                        continue
                job = await store.claim_next(self.lease_seconds)
                if job is None:
                    break
                task = asyncio.create_task(self._run(job), name=f"research-{job['id']}-r{job['revision']}")
                self._workers[task] = (job, "research")

    async def _run(self, job):
        async def checkpoint():
            if not await store.heartbeat(job, self.lease_seconds):
                raise ResearchInterrupted()

        async def progress(phase):
            if not await store.heartbeat(job, self.lease_seconds, phase):
                raise ResearchInterrupted()

        try:
            await checkpoint()
            engine = self.engine
            if engine is None:
                from . import research_engine
                engine = research_engine
            run = engine if callable(engine) else engine.run_research
            async with asyncio.timeout(self.timeout_seconds):
                result = await run(store.effective_request(job), checkpoint=checkpoint, progress=progress)
            await checkpoint()
            # Engine status may report partial evidence; preserve it with answer.
            await store.finish(job, result)
        except ResearchInterrupted:
            await store.release(job)
        except asyncio.CancelledError:
            await store.release(job)
            raise
        except TimeoutError:
            await store.release(job, error="Research exceeded its time budget")
        except Exception:
            await store.release(job, error="Research worker failed; start a new request to try again")

    async def _deliver(self, delivery, bot):
        from . import bot_core
        dispatched = False
        try:
            job = await store.db.fetch_one("SELECT chat_id FROM research_jobs WHERE id = ?", (delivery["job_id"],))
            result = await store.get_job(job["chat_id"], delivery["job_id"])
            if not result or not await store.delivery_current(delivery):
                await store.finish_delivery(delivery, dispatched=False)
                return
            text = format_result(result)
            # The durable sending marker is already committed. From dispatch on,
            # lost acknowledgements are uncertain even if Telegram accepted it.
            dispatched = True
            async with asyncio.timeout(self.send_timeout_seconds):
                message_id = await bot_core.send_research_result(bot, result["chat_id"], text)
            if message_id is None:
                await store.finish_delivery(delivery, error="Delivery acknowledgement unavailable")
            else:
                recorded = await store.finish_delivery(delivery, message_id=message_id)
                if recorded:
                    try:
                        async with asyncio.timeout(5):
                            await bot_core._log_message("sofia", text)
                    except Exception:
                        logger.warning("Optional research conversation log failed after delivery")
        except bot_core.ResearchDeliveryNotStarted:
            await store.finish_delivery(delivery, dispatched=False)
        except asyncio.CancelledError:
            await store.finish_delivery(delivery, error="Delivery interrupted; not retried", dispatched=dispatched)
            raise
        except Exception:
            await store.finish_delivery(delivery, error="Delivery acknowledgement unavailable; not retried", dispatched=dispatched)


def format_result(job):
    """One Telegram-sized plain-text result; full answer stays in status storage."""
    if job["status"] == "failed":
        return f"Research {job['ui_id']} stopped\n\n{job.get('error') or 'Research could not finish within its limits.'}\n\nStatus: /research status {job['ui_id']}"
    partial = (job.get("result") or {}).get("status") == "partial"
    header = f"Research {job['ui_id']}: {'partial findings' if partial else 'complete'}\n\n"
    answer = job.get("answer") or "Research completed without a usable summary."
    sources = []
    for source in job.get("sources", [])[:6]:
        if isinstance(source, dict) and source.get("url") and source["url"] not in answer:
            sources.append(str(source["url"])[:350])
        elif isinstance(source, str) and source not in answer:
            sources.append(source[:350])
    # Engine answers already carry citation IDs and their precise source list.
    # A generic tail is only useful for simpler injected engines with no list.
    tail = "\n\nSources:\n" + "\n".join(sources[:4]) if sources and "Sources" not in answer and "[E" not in answer else ""
    limit = 3900 - len(header) - len(tail)
    if len(answer) > limit:
        suffix = f"\n\nMore details: /research status {job['ui_id']}"
        answer = answer[:max(0, limit - len(suffix))] + suffix
    return header + answer + tail
