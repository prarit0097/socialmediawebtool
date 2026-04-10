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


NOISY_MEDIA_PATTERNS = [
    re.compile(r"\bviral\b", re.IGNORECASE),
    re.compile(r"\breels?\b", re.IGNORECASE),
    re.compile(r"\bofficial\d*\b", re.IGNORECASE),
    re.compile(r"\bdigital\s*ceo\b", re.IGNORECASE),
    re.compile(r"\bcreate[_\s-]*an[_\s-]*\d+[_\s-]*\d+\b", re.IGNORECASE),
    re.compile(r"\bpost\d+\b", re.IGNORECASE),
]


def ai_is_configured() -> bool:
    return bool(settings.AI_API_KEY.strip())


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

    if settings.AI_FALLBACK_MODEL and settings.AI_FALLBACK_MODEL != settings.AI_MODEL:
        candidates.append(
            {
                "model": settings.AI_FALLBACK_MODEL,
                "base_url": settings.AI_API_BASE_URL,
                "api_key": settings.AI_API_KEY,
            }
        )

    return candidates


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _clean_media_name_context(file_name: str) -> str:
    stem = Path(file_name or "").stem
    text = stem.replace("_", " ").replace("-", " ")
    for pattern in NOISY_MEDIA_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"[^A-Za-z0-9\s]+", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _target_niche(target: PublishingTarget) -> str:
    if target.sync_key in settings.AI_TARGET_NICHE_MAP:
        return settings.AI_TARGET_NICHE_MAP[target.sync_key]
    if str(target.pk) in settings.AI_TARGET_NICHE_MAP:
        return settings.AI_TARGET_NICHE_MAP[str(target.pk)]
    return settings.AI_DEFAULT_NICHE


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


def _coerce_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(_coerce_text(item) for item in value if _coerce_text(item)).strip()
    if isinstance(value, dict):
        return value.get("text", "") if isinstance(value.get("text"), str) else json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _coerce_list(value, *, prefix: str = "") -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = [_coerce_text(item) for item in value]
    else:
        text = _coerce_text(value)
        if prefix == "#":
            text = text.replace(",", " ")
            items = [token.strip() for token in text.split() if token.strip()]
        else:
            text = text.replace("\n", ",")
            items = [token.strip() for token in text.split(",") if token.strip()]
    cleaned = []
    for item in items:
        if not item:
            continue
        token = item
        if prefix == "#" and not token.startswith("#"):
            token = f"#{token.lstrip('#')}"
        cleaned.append(token)
    return cleaned


def _normalize_ai_payload(payload: dict, target: PublishingTarget, file_obj: dict, best_times: list[str], best_reason: str) -> dict:
    normalized = dict(payload or {})
    normalized["primary_caption"] = _coerce_text(normalized.get("primary_caption"))
    normalized["short_caption"] = _coerce_text(normalized.get("short_caption"))
    normalized["long_caption"] = _coerce_text(normalized.get("long_caption"))
    normalized["hindi_caption"] = _coerce_text(normalized.get("hindi_caption"))
    normalized["english_caption"] = _coerce_text(normalized.get("english_caption"))
    normalized["hinglish_caption"] = _coerce_text(normalized.get("hinglish_caption"))
    normalized["translated_hindi"] = _coerce_text(normalized.get("translated_hindi"))
    normalized["translated_english"] = _coerce_text(normalized.get("translated_english"))
    normalized["translated_hinglish"] = _coerce_text(normalized.get("translated_hinglish"))
    normalized["report_summary"] = _coerce_text(normalized.get("report_summary"))
    normalized["duplicate_reason"] = _coerce_text(normalized.get("duplicate_reason"))
    normalized["best_posting_reason"] = _coerce_text(normalized.get("best_posting_reason")) or best_reason
    normalized["primary_category"] = _coerce_text(normalized.get("primary_category")) or "general"
    normalized["hashtags"] = _coerce_list(normalized.get("hashtags"), prefix="#")
    normalized["secondary_tags"] = _coerce_list(normalized.get("secondary_tags"))
    normalized["quality_issues"] = _coerce_list(normalized.get("quality_issues"))
    normalized["best_posting_times"] = _coerce_list(normalized.get("best_posting_times")) or best_times
    normalized["safe_to_post"] = bool(normalized.get("safe_to_post", True))
    return normalized


def _payload_quality_errors(payload: dict, file_obj: dict) -> list[str]:
    issues = []
    file_stem = Path(file_obj.get("name", "")).stem.strip().lower()
    cleaned_context = _clean_media_name_context(file_obj.get("name", "")).lower()
    primary_caption = _coerce_text(payload.get("primary_caption"))
    if not primary_caption:
        issues.append("primary_caption missing")
    elif file_stem and primary_caption.strip().lower() == file_stem:
        issues.append("primary_caption looks like raw filename")
    elif cleaned_context and _normalize_text(primary_caption) == _normalize_text(cleaned_context):
        issues.append("primary_caption mirrors cleaned filename context too closely")

    hashtags = _coerce_list(payload.get("hashtags"), prefix="#")
    if len(hashtags) < 2:
        issues.append("not enough hashtags")

    rewrite_fields = [
        _coerce_text(payload.get("short_caption")),
        _coerce_text(payload.get("long_caption")),
        _coerce_text(payload.get("hindi_caption")),
        _coerce_text(payload.get("english_caption")),
        _coerce_text(payload.get("hinglish_caption")),
        _coerce_text(payload.get("translated_hindi")),
        _coerce_text(payload.get("translated_english")),
        _coerce_text(payload.get("translated_hinglish")),
    ]
    populated_rewrites = sum(1 for value in rewrite_fields if value and value != "-")
    if populated_rewrites < 4:
        issues.append("too many rewrite/translation fields missing")

    return issues


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
    current_name = _clean_media_name_context(file_obj.get("name", ""))
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
        name_tokens = set(_normalize_text(_clean_media_name_context(name)).split())
        if current_tokens and current_tokens == name_tokens:
            return "high", f"Filename pattern matches a previously published media item: {name}"
        if current_tokens and len(current_tokens & name_tokens) >= max(2, len(current_tokens) // 2):
            return "medium", f"Filename shares many tokens with previous media item: {name}"
    return "low", "No close duplicate pattern found in recent published filenames."


def _quality_signal(target: PublishingTarget, file_obj: dict) -> tuple[str, list[str], bool]:
    issues = []
    name = file_obj.get("name", "")
    mime_type = file_obj.get("mimeType", "")
    cleaned_context = _clean_media_name_context(name)
    if len(_normalize_text(cleaned_context)) < 6:
        issues.append("File name is too generic; AI context may be weak.")
    if cleaned_context != Path(name).stem.strip():
        issues.append("Filename looks automation-generated; ignore it as content inspiration.")
    if mime_type.startswith("video/") and ("viral" in name.lower() or "official" in name.lower()):
        issues.append("Video name looks highly templated; consider checking originality.")
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
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                parsed.setdefault(
                    "_ai_meta",
                    {
                        "provider_base_url": base_url,
                        "requested_model": model_name,
                        "resolved_model": request_model,
                    },
                )
            return parsed
        except json.JSONDecodeError as exc:
            errors.append(f"{model_name}: AI response was not valid JSON: {exc}")

    raise AIServiceError(" | ".join(errors))


def _ai_payload_from_context(target: PublishingTarget, file_obj: dict) -> dict:
    best_times, best_reason = _best_posting_time_stats(target)
    duplicate_risk, duplicate_reason = _duplicate_signal(target, file_obj)
    quality_risk, quality_issues, safe_to_post = _quality_signal(target, file_obj)

    file_name = file_obj.get("name", "")
    cleaned_context = _clean_media_name_context(file_name)
    system_prompt = (
        "You generate social media strategy JSON for a Django posting app. "
        "Return strict JSON with keys: primary_caption, hashtags, short_caption, long_caption, "
        "hindi_caption, english_caption, hinglish_caption, primary_category, secondary_tags, "
        "duplicate_risk, duplicate_reason, quality_risk, quality_issues, safe_to_post, "
        "translated_hindi, translated_english, translated_hinglish, best_posting_times, "
        "best_posting_reason, report_summary. Keep captions concise and platform-safe. "
        "Do not echo raw filenames, serial numbers, 'viral', 'official', or automation wording unless present in the provided human caption."
    )
    user_prompt = (
        f"Profile: {target.display_name}\n"
        f"Tone: {target.ai_tone}\n"
        f"Preferred language: {target.ai_language}\n"
        f"Content niche: {_target_niche(target)}\n"
        f"Existing default caption: {target.default_caption[:1500]}\n"
        f"File name: {file_name}\n"
        f"File type: {file_obj.get('mimeType', '')}\n"
        f"Cleaned media context: {cleaned_context or 'unknown'}\n"
        f"Heuristic duplicate risk: {duplicate_risk} ({duplicate_reason})\n"
        f"Heuristic quality risk: {quality_risk} ({'; '.join(quality_issues) or 'no issues'})\n"
        f"Heuristic best posting times: {best_times} ({best_reason})\n"
        "Generate smart posting guidance for this exact niche. Favor original, human-sounding, non-spammy copy."
    )
    candidates = _build_model_candidates()
    candidate_errors = []
    for index, candidate in enumerate(candidates):
        original_model = settings.AI_MODEL
        original_base = settings.AI_API_BASE_URL
        original_key = settings.AI_API_KEY
        original_fallback_model = settings.AI_FALLBACK_MODEL
        try:
            settings.AI_MODEL = candidate["model"]
            settings.AI_API_BASE_URL = candidate["base_url"]
            settings.AI_API_KEY = candidate["api_key"]
            settings.AI_FALLBACK_MODEL = ""
            ai_data = _call_openai_json(system_prompt, user_prompt)
            ai_data = _normalize_ai_payload(ai_data, target, file_obj, best_times, best_reason)
            quality_errors = _payload_quality_errors(ai_data, file_obj)
            if quality_errors and index < len(candidates) - 1:
                candidate_errors.append(f"{candidate['model']}: weak output ({', '.join(quality_errors)})")
                continue
            ai_data.setdefault("duplicate_risk", duplicate_risk)
            ai_data.setdefault("duplicate_reason", duplicate_reason)
            ai_data.setdefault("quality_risk", quality_risk)
            ai_data.setdefault("quality_issues", quality_issues)
            ai_data.setdefault("safe_to_post", safe_to_post)
            ai_data.setdefault("best_posting_times", best_times)
            ai_data.setdefault("best_posting_reason", best_reason)
            if quality_errors:
                ai_data["quality_issues"] = list(dict.fromkeys(_coerce_list(ai_data.get("quality_issues")) + quality_errors))
            return ai_data
        except AIServiceError as exc:
            candidate_errors.append(str(exc))
        finally:
            settings.AI_MODEL = original_model
            settings.AI_API_BASE_URL = original_base
            settings.AI_API_KEY = original_key
            settings.AI_FALLBACK_MODEL = original_fallback_model

    raise AIServiceError(" | ".join(candidate_errors))


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
