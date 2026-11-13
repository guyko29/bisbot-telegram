"""Scheduled summaries: every day at 21:00 and every Friday at 12:00 (local time)."""

from __future__ import annotations

import logging
from datetime import datetime, time

from telegram.ext import Application, ContextTypes

from .config import Settings
from .handlers import split_message
from .reports import Reports

logger = logging.getLogger(__name__)

DAILY_JOB = "daily-summary"
WEEKLY_JOB = "weekly-summary"


def schedule_jobs(application: Application, settings: Settings, reports: Reports) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError(
            "JobQueue is unavailable - install python-telegram-bot[job-queue]"
        )

    async def send(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        for part in split_message(text):
            await context.bot.send_message(chat_id=settings.allowed_chat_id, text=part)

    async def daily(context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info("Sending the daily summary")
        await send(context, await reports.daily_report(datetime.now(settings.tz)))

    async def weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info("Sending the weekly summary")
        await send(context, await reports.weekly_report(datetime.now(settings.tz)))

    def local(moment: time) -> time:
        return moment.replace(tzinfo=settings.tz)

    job_queue.run_daily(daily, time=local(settings.daily_summary_time), name=DAILY_JOB)
    job_queue.run_daily(
        weekly,
        time=local(settings.weekly_summary_time),
        days=(settings.weekly_summary_weekday,),
        name=WEEKLY_JOB,
    )
