from __future__ import annotations

from datetime import datetime, timedelta
import re

import requests
from django.conf import settings
from django.utils import timezone

from scheduler.models import DailyReportLog, PostLog, PublishingTarget
from scheduler.services.ai import ai_is_configured, build_ai_report_summary


def _day_bounds(report_date):
    start = timezone.make_aware(datetime.combine(report_date, datetime.min.time()))
    end = start + timedelta(days=1)
    return start, end


def _target_day_stats(target, report_date):
    start, end = _day_bounds(report_date)
    success_logs = list(
        target.post_logs.filter(
            status=PostLog.STATUS_SUCCESS,
            published_at__gte=start,
            published_at__lt=end,
        ).values("platform", "drive_file_name", "published_at")
    )
    failed_logs = list(
        target.post_logs.filter(
            status=PostLog.STATUS_FAILED,
            created_at__gte=start,
            created_at__lt=end,
        ).values("platform", "drive_file_name", "message", "created_at")
    )
    return success_logs, failed_logs


def _short_reason(message: str, limit: int = 120) -> str:
    text = (message or "").replace("\n", " ").strip()
    markers = [
        "Possible causes:",
        "Diagnostics:",
    ]
    for marker in markers:
        if marker in text:
            text = text.split(marker, 1)[0].strip(" |")
    text = re.sub(r"https?://\S+", "[url removed]", text)
    return text[:limit].rstrip()


def build_daily_report_message(report_date):
    now = timezone.localtime()
    active_targets = list(PublishingTarget.objects.filter(is_active=True).order_by("display_name"))
    active_targets_count = len(active_targets)
    targets_with_activity = 0
    total_success = 0
    total_failed = 0
    successful_targets = []
    attention_targets = []

    for target in active_targets:
        success_logs, failed_logs = _target_day_stats(target, report_date)
        success_count = len(success_logs)
        failed_count = len(failed_logs)
        if success_count or failed_count:
            targets_with_activity += 1
        total_success += success_count
        total_failed += failed_count

        if success_count:
            file_names = ", ".join(dict.fromkeys(log["drive_file_name"] for log in success_logs if log["drive_file_name"])) or "-"
            fb_success = sum(1 for log in success_logs if log["platform"] == "facebook")
            ig_success = sum(1 for log in success_logs if log["platform"] == "instagram")
            successful_targets.append(
                f"- {target.display_name}: FB {fb_success} | IG {ig_success} | Files: {file_names}"
            )

        if failed_count:
            first_failure = failed_logs[0]
            attention_targets.append(
                f"- {target.display_name}: Failed {failed_count} | Last file: {first_failure.get('drive_file_name') or '-'} | Reason: {_short_reason(first_failure.get('message', ''))}"
            )

    quiet_targets = active_targets_count - targets_with_activity
    health_line = "Healthy"
    if total_failed and total_success:
        health_line = "Partial issues"
    elif total_failed and not total_success:
        health_line = "Attention needed"

    lines = [
        "SOCIAL POSTING REPORT",
        f"Report Date: {report_date:%d %b %Y}",
        f"Generated At: {now:%d %b %Y, %I:%M %p}",
        "",
        "OVERVIEW",
        f"- Overall health: {health_line}",
        f"- Active targets: {active_targets_count}",
        f"- Targets with activity: {targets_with_activity}",
        f"- Successful publishes: {total_success}",
        f"- Failed publishes: {total_failed}",
        f"- Quiet targets: {quiet_targets}",
    ]

    if successful_targets:
        lines.extend(["", "SUCCESSFUL ACTIVITY"])
        lines.extend(successful_targets[:10])

    if attention_targets:
        lines.extend(["", "NEEDS ATTENTION"])
        lines.extend(attention_targets[:10])

    if quiet_targets:
        lines.extend(["", f"NOTE: {quiet_targets} active target(s) had no posting activity on this date."])

    if not active_targets:
        lines.extend(["", "No active targets configured."])

    if ai_is_configured():
        try:
            ai_summary = build_ai_report_summary(report_date, lines)
        except Exception:
            ai_summary = ""
        if ai_summary:
            lines.extend(["", "AI SUMMARY", ai_summary])

    return "\n".join(lines)


def send_telegram_message(message: str) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        raise ValueError("Telegram bot token or chat ID is not configured.")
    response = requests.post(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": message},
        timeout=30,
    )
    response.raise_for_status()


def send_daily_report(force=False, report_date=None):
    now = timezone.localtime()
    report_date = report_date or (now.date() - timedelta(days=1))
    report_log, _ = DailyReportLog.objects.get_or_create(report_date=report_date)
    already_sent_today = False
    if report_log.sent_at:
        already_sent_today = timezone.localtime(report_log.sent_at).date() == now.date()
    if already_sent_today and not force:
        return {"status_message": f"Report for {report_date:%Y-%m-%d} was already sent."}
    message = build_daily_report_message(report_date)
    send_telegram_message(message)
    report_log.sent_at = timezone.now()
    report_log.status = "sent"
    report_log.telegram_chat_id = settings.TELEGRAM_CHAT_ID
    report_log.message = message
    report_log.save()
    return {"status_message": f"Report sent for {report_date:%Y-%m-%d}."}
