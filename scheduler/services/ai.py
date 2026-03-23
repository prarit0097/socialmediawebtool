from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import requests
from django.conf import settings
from django.utils import timezone

from scheduler.models import AIMediaInsight, PostLog, PublishingTarget, SocialAccount
from scheduler.services.drive import is_publishable_media, list_folder_files


class AIServiceError(Exception):
    pass


def ai_is_configured() -> bool:
    return bool(settings.AI_API_KEY.strip() or settings.AI_FALLBACK_API_KEY.strip())


def _resolve_model_name(model_name: str, base_url: str) -> str:
    model_name = (model_name or "").strip()
    if not model_name:
        return model_name
    if base_url.rstrip("/").lower().startswith("https://api.openai.com"):
        return model_name.split("/", 1)[1] if "/" in model_name else model_name
    return model_name


def _build_model_candidates() -> list[dict]:
    candidates = []
    primary = {
        "model": settings.AI_MODEL,
        "base_url": settings.AI_API_BASE_URL,
        "api_key": settings.AI_API_KEY,
    }
    if primary["model"] and primary["base_url"] and primary["api_key"]:
        candidates.append(primary)

    fallback = {
        "model": settings.AI_FALLBACK_MODEL,
        "base_url": settings.AI_FALLBACK_API_BASE_URL or settings.AI_API_BASE_URL,
        "api_key": settings.AI_FALLBACK_API_KEY or settings.AI_API_KEY,
    }
    if fallback["model"] and fallback["base_url"] and fallback["api_key"]:
        if not candidates or fallback != candidates[0]:
            candidates.append(fallback)

    return candidates


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _json_response_text(data: dict) -> str:
    if data.get("output_text"):
        return data["output_text"]
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text") or content.get("output_text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def _strip_json_block(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def _best_posting_time_stats(target: PublishingTarget) -> tuple[list[str], str]:
    success_logs = target.post_logs.filter(status=PostLog.STATUS_SUCCESS).exclude(published_at=None).values_list("scheduled_for", flat=True)
    counts = Counter()
    for scheduled_for in success_logs:
        counts[timezone.localtime(scheduled_for).strftime("%H:%M")] += 1
    if counts:
        top = [time for time, _ in counts.most_common(3)]
        return top, f"Past success counts by slot: {dict(counts)}"
    fallback_times = target.posting_times[:3]
    if not fallback_times:
        fallback_times = [target.posting_window_start.strftime("%H:%M")]
    return fallback_times, "No past success data available yet, so current configured slots are recommended."


def _duplicate_signal(target: PublishingTarget, file_obj: dict) -> tuple[str, str]:
    current_name = Path(file_obj.get("name", "")).stem
    current_tokens = set(_normalize_text(current_name).split())
    if not current_tokens:
        return "low", "No strong duplicate signal from filename."

    recent_names = list(
        target.post_logs.filter(status=PostLog.STATUS_SUCCESS)
        .exclude(drive_file_name="")
        .order_by("-created_at")
        .values_list("drive_file_name", flat=True)[:25]
    )
    for name in recent_names:
        name_tokens = set(_normalize_text(Path(name).stem).split())
        if current_tokens and current_tokens == name_tokens:
            return "high", f"Filename pattern matches a previously published media item: {name}"
        if current_tokens and len(current_tokens & name_tokens) >= max(2, len(current_tokens) // 2):
            return "medium", f"Filename shares many tokens with previous media item: {name}"
    return "low", "No close duplicate pattern found in recent published filenames."


def _quality_signal(target: PublishingTarget, file_obj: dict) -> tuple[str, list[str], bool]:
    issues = []
    name = file_obj.get("name", "")
    mime_type = file_obj.get("mimeType", "")
    if len(_normalize_text(Path(name).stem)) < 6:
        issues.append("File name is too generic; AI context may be weak.")
    if mime_type.startswith("video/") and "viral" in name.lower():
        issues.append("Video looks highly templated; consider checking originality.")
    if not target.default_caption.strip():
        issues.append("Default caption is empty; AI should generate one before posting.")
    if not issues:
        return "low", [], True
    risk = "medium" if len(issues) == 1 else "high"
    return risk, issues, risk != "high"


def _next_candidate_file(target: PublishingTarget) -> dict:
    files = list_folder_files(target.drive_folder_id)
    media_files = [file_obj for file_obj in files if is_publishable_media(file_obj)]
    active_platforms = set()
    if target.facebook_account_id:
        active_platforms.add(SocialAccount.FACEBOOK)
    if target.instagram_account_id:
        active_platforms.add(SocialAccount.INSTAGRAM)

    success_rows = list(
        target.post_logs.filter(status=PostLog.STATUS_SUCCESS)
        .exclude(drive_file_id="")
        .values("drive_file_id", "platform")
    )
    success_map: dict[str, set[str]] = {}
    for row in success_rows:
        success_map.setdefault(row["drive_file_id"], set()).add(row["platform"])

    for file_obj in media_files:
        if success_map.get(file_obj["id"], set()) != active_platforms:
            return file_obj
    if media_files:
        return media_files[-1]
    raise AIServiceError("No media files available in the configured Drive folder.")


def _call_openai_json(system_prompt: str, user_prompt: str) -> dict:
    if not ai_is_configured():
        raise AIServiceError("No AI provider credentials are configured.")

    candidates = _build_model_candidates()
    if not candidates:
        raise AIServiceError("No AI model/base URL/api key combination is configured.")

    errors = []
    for candidate in candidates:
        model_name = candidate["model"]
        base_url = candidate["base_url"]
        api_key = candidate["api_key"]
        request_model = _resolve_model_name(model_name, base_url)
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": request_model,
                    "input": [
                        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                        {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
                    ],
                },
                timeout=settings.AI_TIMEOUT_SECONDS,
            )
            data = response.json()
        except requests.RequestException as exc:
            errors.append(f"{model_name}: request failed: {exc}")
            continue
        except ValueError as exc:
            errors.append(f"{model_name}: response JSON parse failed: {exc}")
            continue
        if response.status_code >= 400 or data.get("error"):
            message = data.get("error", {}).get("message", response.text)
            errors.append(f"{model_name}: {message}")
            continue

        raw_text = _strip_json_block(_json_response_text(data))
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            errors.append(f"{model_name}: AI response was not valid JSON: {exc}")

    raise AIServiceError(" | ".join(errors))


def _ai_payload_from_context(target: PublishingTarget, file_obj: dict) -> dict:
    best_times, best_reason = _best_posting_time_stats(target)
    duplicate_risk, duplicate_reason = _duplicate_signal(target, file_obj)
    quality_risk, quality_issues, safe_to_post = _quality_signal(target, file_obj)

    file_name = file_obj.get("name", "")
    file_stem = Path(file_name).stem
    system_prompt = (
        "You generate social media strategy JSON for a Django posting app. "
        "Return strict JSON with keys: primary_caption, hashtags, short_caption, long_caption, "
        "hindi_caption, english_caption, hinglish_caption, primary_category, secondary_tags, "
        "duplicate_risk, duplicate_reason, quality_risk, quality_issues, safe_to_post, "
        "translated_hindi, translated_english, translated_hinglish, best_posting_times, "
        "best_posting_reason, report_summary. Keep captions concise and platform-safe."
    )
    user_prompt = (
        f"Profile: {target.display_name}\n"
        f"Tone: {target.ai_tone}\n"
        f"Preferred language: {target.ai_language}\n"
        f"Existing default caption: {target.default_caption[:1500]}\n"
        f"File name: {file_name}\n"
        f"File type: {file_obj.get('mimeType', '')}\n"
        f"Derived title context: {file_stem}\n"
        f"Heuristic duplicate risk: {duplicate_risk} ({duplicate_reason})\n"
        f"Heuristic quality risk: {quality_risk} ({'; '.join(quality_issues) or 'no issues'})\n"
        f"Heuristic best posting times: {best_times} ({best_reason})\n"
        "Generate smart posting guidance for health/wellness social media use."
    )
    ai_data = _call_openai_json(system_prompt, user_prompt)
    ai_data.setdefault("duplicate_risk", duplicate_risk)
    ai_data.setdefault("duplicate_reason", duplicate_reason)
    ai_data.setdefault("quality_risk", quality_risk)
    ai_data.setdefault("quality_issues", quality_issues)
    ai_data.setdefault("safe_to_post", safe_to_post)
    ai_data.setdefault("best_posting_times", best_times)
    ai_data.setdefault("best_posting_reason", best_reason)
    return ai_data


def get_or_generate_media_insight(target: PublishingTarget, file_obj: dict | None = None, force: bool = False) -> AIMediaInsight:
    file_obj = file_obj or _next_candidate_file(target)
    insight, _ = AIMediaInsight.objects.get_or_create(
        target=target,
        drive_file_id=file_obj["id"],
        defaults={
            "drive_file_name": file_obj.get("name", ""),
            "source_mime_type": file_obj.get("mimeType", ""),
        },
    )
    if insight.last_analyzed_at and not force and insight.primary_caption.strip():
        return insight

    duplicate_risk, duplicate_reason = _duplicate_signal(target, file_obj)
    quality_risk, quality_issues, safe_to_post = _quality_signal(target, file_obj)
    best_times, best_reason = _best_posting_time_stats(target)

    payload = {
        "primary_caption": target.default_caption.strip(),
        "hashtags": [],
        "short_caption": target.default_caption.strip(),
        "long_caption": target.default_caption.strip(),
        "hindi_caption": "",
        "english_caption": "",
        "hinglish_caption": target.default_caption.strip(),
        "primary_category": "general",
        "secondary_tags": [],
        "duplicate_risk": duplicate_risk,
        "duplicate_reason": duplicate_reason,
        "quality_risk": quality_risk,
        "quality_issues": quality_issues,
        "safe_to_post": safe_to_post,
        "translated_hindi": "",
        "translated_english": "",
        "translated_hinglish": target.default_caption.strip(),
        "best_posting_times": best_times,
        "best_posting_reason": best_reason,
        "report_summary": "",
    }
    if ai_is_configured() and target.ai_enabled:
        payload = _ai_payload_from_context(target, file_obj)

    insight.drive_file_name = file_obj.get("name", "")
    insight.source_mime_type = file_obj.get("mimeType", "")
    insight.primary_category = payload.get("primary_category", "")
    insight.secondary_tags = payload.get("secondary_tags", [])
    insight.primary_caption = payload.get("primary_caption", "")
    insight.hashtags = payload.get("hashtags", [])
    insight.short_caption = payload.get("short_caption", "")
    insight.long_caption = payload.get("long_caption", "")
    insight.hindi_caption = payload.get("hindi_caption", "")
    insight.english_caption = payload.get("english_caption", "")
    insight.hinglish_caption = payload.get("hinglish_caption", "")
    insight.duplicate_risk = payload.get("duplicate_risk", duplicate_risk)
    insight.duplicate_reason = payload.get("duplicate_reason", duplicate_reason)
    insight.quality_risk = payload.get("quality_risk", quality_risk)
    insight.quality_issues = payload.get("quality_issues", quality_issues)
    insight.safe_to_post = payload.get("safe_to_post", safe_to_post)
    insight.translated_hindi = payload.get("translated_hindi", "")
    insight.translated_english = payload.get("translated_english", "")
    insight.translated_hinglish = payload.get("translated_hinglish", "")
    insight.best_posting_times = payload.get("best_posting_times", best_times)
    insight.best_posting_reason = payload.get("best_posting_reason", best_reason)
    insight.report_summary = payload.get("report_summary", "")
    insight.raw_payload = payload
    insight.last_error = ""
    insight.last_analyzed_at = timezone.now()
    insight.save()

    target.ai_last_generated_at = timezone.now()
    if insight.report_summary:
        target.ai_last_report_summary = insight.report_summary
    target.save(update_fields=["ai_last_generated_at", "ai_last_report_summary", "updated_at"])
    return insight


def build_ai_caption_for_media(target: PublishingTarget, file_obj: dict) -> str:
    insight = get_or_generate_media_insight(target, file_obj=file_obj)
    caption_parts = [insight.primary_caption.strip()]
    if insight.hashtags:
        caption_parts.append(" ".join(insight.hashtags))
    return "\n\n".join(part for part in caption_parts if part).strip()


def build_ai_report_summary(report_date, structured_lines: list[str]) -> str:
    if not ai_is_configured():
        return ""

    system_prompt = (
        "You summarize daily social posting reports for a business owner. "
        "Return strict JSON with keys report_summary and action_items. "
        "Keep the summary concise, professional, and practical."
    )
    user_prompt = (
        f"Report date: {report_date}\n"
        "Structured report lines:\n"
        + "\n".join(structured_lines[:80])
    )
    payload = _call_openai_json(system_prompt, user_prompt)
    summary = payload.get("report_summary", "").strip()
    action_items = payload.get("action_items", [])
    if action_items:
        summary += "\nAction items: " + " | ".join(action_items[:3])
    return summary.strip()
