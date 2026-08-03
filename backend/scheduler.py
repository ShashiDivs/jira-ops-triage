"""Scheduled ticket generation: an APScheduler job that periodically submits a synthetic
ticket through the exact same entry point the dashboard's "Submit ticket" button uses.

Teaching point: agents don't only respond to a human click — they can run on their own clock.
This deliberately reuses the identical pipeline (same graph, same tracing) rather than adding a
separate code path, and deliberately does NOT touch historical_tickets (the RAG seed corpus) —
scheduled tickets land in the normal tickets/agent_runs tables like any other ticket. Promoting
a resolved ticket into the RAG corpus is a separate, manual action, not a side effect of a timer.
"""
import logging
import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.runtime import get_graph
from config import get_settings
from mcp_server import jira_store

logger = logging.getLogger("scheduler")
settings = get_settings()

_JOB_ID = "synthetic_ticket_generator"

# A pool spanning every severity/category the pipeline can classify, so a class watching the
# dashboard over time sees auto-approved, HITL-paused, security, and cosmetic cases all show up
# on their own. None of these duplicate the RAG seed corpus verbatim (backend/data/*.json).
SAMPLE_TICKETS: list[dict[str, str]] = [
    {"summary": "Complete outage of payments-service, all checkouts failing", "description": "payments-service is timing out on every request during checkout.", "component": "payments-service"},
    {"summary": "Suspicious login attempts detected on employee VPN", "description": "Multiple failed logins followed by a successful one, from an unrecognized country, for a privileged account.", "component": "auth-service"},
    {"summary": "Customer SSNs visible in analytics dashboard", "description": "An internal analytics report accidentally includes unmasked social security numbers for a batch of customers.", "component": "support-dashboard"},
    {"summary": "Duplicate refunds issued to customers", "description": "A refund job re-ran overnight and issued a second refund to roughly 15 customers.", "component": "billing-service"},
    {"summary": "API key accidentally committed to a public repo", "description": "A service API key was found committed in a public GitHub repository.", "component": "auth-service"},
    {"summary": "Checkout latency spiking during peak hours", "description": "p95 checkout latency has tripled since this morning's deploy.", "component": "checkout-service"},
    {"summary": "Intermittent 500s on the recommendations API", "description": "The recommendations API returns 500 for about 5% of requests since last night.", "component": "recommendations-service"},
    {"summary": "Elevated error rate on the auth-service login endpoint", "description": "Login endpoint error rate has climbed to 3%, up from a baseline of 0.1%.", "component": "auth-service"},
    {"summary": "Search autocomplete returning empty results intermittently", "description": "About 1 in 10 autocomplete requests return no suggestions.", "component": "search-service"},
    {"summary": "Dashboard widget fails to load for large teams", "description": "Teams with more than 200 members see a blank widget instead of the usage chart.", "component": "profile-service"},
    {"summary": "Push notifications delayed for iOS users", "description": "iOS push notifications are arriving 5-10 minutes late; Android is unaffected.", "component": "notifications-service"},
    {"summary": "Export to CSV times out for large datasets", "description": "Exporting more than 10,000 rows to CSV times out client-side.", "component": "profile-service"},
    {"summary": "Slack integration stops posting after a few hours", "description": "The Slack integration silently stops delivering notifications after roughly 4 hours of uptime.", "component": "notifications-service"},
    {"summary": "Broken image icon in email footer", "description": "The social media icon in the transactional email footer shows as a broken image link.", "component": "notifications-service"},
    {"summary": "Misaligned button on settings page", "description": "The Save button on the settings page overlaps other controls on mobile screens.", "component": "profile-service"},
    {"summary": "Outdated copyright year on pricing page", "description": "The pricing page footer still shows last year's copyright year.", "component": "marketing-site"},
    {"summary": "Typo in onboarding tooltip", "description": "The onboarding tooltip reads 'Your'e all set!' with a misplaced apostrophe.", "component": "marketing-site"},
]


async def _generate_and_submit_ticket() -> None:
    template = random.choice(SAMPLE_TICKETS)
    ticket = jira_store.create_ticket(
        summary=template["summary"],
        description=template["description"],
        component=template["component"],
        reporter="scheduler",
        status="pending",
    )
    ticket_id = ticket["id"]
    config = {"configurable": {"thread_id": ticket_id}}

    try:
        result = await get_graph().ainvoke(
            {
                "ticket_id": ticket_id,
                "summary": template["summary"],
                "description": template["description"],
                "component": template["component"],
                "reporter": "scheduler",
            },
            config=config,
        )
        state = "pending (HITL)" if result.get("__interrupt__") else "completed"
        logger.info("scheduler submitted ticket %s (%s): %s", ticket_id, state, template["summary"])
    except Exception:
        logger.exception("scheduler failed to process ticket %s", ticket_id)


_scheduler: AsyncIOScheduler | None = None


def init_scheduler() -> None:
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("ticket scheduler disabled (SCHEDULER_ENABLED=false)")
        return
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _generate_and_submit_ticket,
        "interval",
        seconds=settings.scheduler_interval_seconds,
        id=_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("ticket scheduler started: every %ss", settings.scheduler_interval_seconds)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def pause_scheduler() -> None:
    if _scheduler is not None:
        _scheduler.pause_job(_JOB_ID)


def resume_scheduler() -> None:
    if _scheduler is not None:
        _scheduler.resume_job(_JOB_ID)


def scheduler_status() -> dict:
    if _scheduler is None:
        return {"enabled": settings.scheduler_enabled, "running": False, "paused": None, "interval_seconds": settings.scheduler_interval_seconds}
    job = _scheduler.get_job(_JOB_ID)
    return {
        "enabled": settings.scheduler_enabled,
        "running": True,
        "paused": job is not None and job.next_run_time is None,
        "interval_seconds": settings.scheduler_interval_seconds,
    }
