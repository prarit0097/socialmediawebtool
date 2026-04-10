from __future__ import annotations

import csv
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, SocialAccount
from scheduler.services.publishing import PublishingError, _graph_get


def _facebook_token_for_log(log: PostLog) -> str:
    target = log.target
    return (target.facebook_account.access_token if target.facebook_account else "") or target.credential.access_token


def _instagram_token_for_log(log: PostLog) -> str:
    target = log.target
    return (
        (target.instagram_account.access_token if target.instagram_account else "")
        or (target.facebook_account.access_token if target.facebook_account else "")
        or target.credential.access_token
    )


def _safe_graph_get(path: str, access_token: str, params: dict | None = None) -> dict:
    if not access_token:
        raise PublishingError("Access token missing for metrics lookup.")
    return _graph_get(path, access_token, params)


def _facebook_permalink_matches_object_id(permalink: str, object_id: str) -> bool:
    path = urlparse(permalink or "").path.rstrip("/")
    return bool(path and path.endswith(f"/{object_id}"))


def _resolve_facebook_post_and_video_ids(object_id: str, page_id: str, access_token: str) -> tuple[str, str]:
    if not object_id or not page_id:
        return object_id, object_id if object_id and "_" not in object_id else ""
    if "_" in object_id:
        return object_id, ""

    post_items = _safe_graph_get(
        f"/{page_id}/posts",
        access_token,
        {"fields": "id,permalink_url", "limit": 100},
    ).get("data", [])
    for item in post_items:
        if _facebook_permalink_matches_object_id(item.get("permalink_url", ""), object_id):
            return item.get("id", object_id), object_id

    return object_id, object_id


def fetch_facebook_metrics(object_id: str, access_token: str, page_id: str = "") -> dict:
    payload = {
        "id": object_id,
        "permalink_url": "",
        "created_time": "",
        "reaction_count": "",
        "comment_count": "",
        "share_count": "",
        "views": "",
        "impressions": "",
        "reach": "",
        "engaged_users": "",
    }

    post_object_id, video_object_id = _resolve_facebook_post_and_video_ids(object_id, page_id, access_token)

    if post_object_id and "_" in post_object_id:
        primary = _safe_graph_get(
            f"/{post_object_id}",
            access_token,
            {
                "fields": ",".join(
                    [
                        "id",
                        "permalink_url",
                        "created_time",
                        "comments.summary(true)",
                        "reactions.summary(true)",
                        "shares",
                    ]
                )
            },
        )
        payload["id"] = primary.get("id", post_object_id)
        payload["permalink_url"] = primary.get("permalink_url", "")
        payload["created_time"] = primary.get("created_time", "")
        payload["comment_count"] = str(primary.get("comments", {}).get("summary", {}).get("total_count", ""))
        payload["reaction_count"] = str(primary.get("reactions", {}).get("summary", {}).get("total_count", ""))
        payload["share_count"] = str(primary.get("shares", {}).get("count", ""))

    if video_object_id:
        try:
            video = _safe_graph_get(
                f"/{video_object_id}",
                access_token,
                {
                    "fields": ",".join(
                        [
                            "id",
                            "permalink_url",
                            "created_time",
                            "likes.summary(true)",
                            "comments.summary(true)",
                        ]
                    )
                },
            )
        except PublishingError:
            video = {}
        if not payload["id"]:
            payload["id"] = video.get("id", object_id)
        if not payload["permalink_url"]:
            payload["permalink_url"] = video.get("permalink_url", "")
        if not payload["created_time"]:
            payload["created_time"] = video.get("created_time", "")
        if not payload["comment_count"]:
            payload["comment_count"] = str(video.get("comments", {}).get("summary", {}).get("total_count", ""))
        if not payload["reaction_count"]:
            payload["reaction_count"] = str(video.get("likes", {}).get("summary", {}).get("total_count", ""))

    for metric in ("post_impressions", "post_impressions_unique", "post_engaged_users", "total_video_views"):
        try:
            metric_object_id = video_object_id if metric == "total_video_views" and video_object_id else post_object_id
            if not metric_object_id:
                continue
            insights = _safe_graph_get(f"/{metric_object_id}/insights", access_token, {"metric": metric})
        except PublishingError:
            continue
        for item in insights.get("data", []):
            value = item.get("values", [{}])[0].get("value", "")
            if metric == "post_impressions":
                payload["impressions"] = str(value)
            elif metric == "post_impressions_unique":
                payload["reach"] = str(value)
            elif metric == "post_engaged_users":
                payload["engaged_users"] = str(value)
            elif metric == "total_video_views":
                payload["views"] = str(value)
    return payload


def fetch_instagram_metrics(media_id: str, access_token: str) -> dict:
    payload = {
        "id": media_id,
        "permalink": "",
        "timestamp": "",
        "media_type": "",
        "media_product_type": "",
        "like_count": "",
        "comments_count": "",
        "impressions": "",
        "reach": "",
        "saved": "",
        "shares": "",
        "views": "",
        "total_interactions": "",
    }

    media = _safe_graph_get(
        f"/{media_id}",
        access_token,
        {
            "fields": ",".join(
                [
                    "id",
                    "permalink",
                    "timestamp",
                    "media_type",
                    "media_product_type",
                    "like_count",
                    "comments_count",
                ]
            )
        },
    )
    for key in ("id", "permalink", "timestamp", "media_type", "media_product_type", "like_count", "comments_count"):
        payload[key] = str(media.get(key, ""))

    for metric in ("impressions", "reach", "saved", "shares", "views", "total_interactions"):
        try:
            insights = _safe_graph_get(f"/{media_id}/insights", access_token, {"metric": metric})
        except PublishingError:
            continue
        for item in insights.get("data", []):
            payload[metric] = str(item.get("values", [{}])[0].get("value", ""))
    return payload


def iter_tool_post_metrics(*, target: PublishingTarget | None = None, days: int = 7) -> list[dict]:
    since = timezone.now() - timedelta(days=days)
    queryset = (
        PostLog.objects.filter(status=PostLog.STATUS_SUCCESS, published_at__gte=since)
        .exclude(meta_creation_id="")
        .select_related("target__credential", "target__facebook_account", "target__instagram_account")
        .order_by("-published_at")
    )
    if target is not None:
        queryset = queryset.filter(target=target)

    rows = []
    for log in queryset:
        try:
            if log.platform == SocialAccount.FACEBOOK:
                metrics = fetch_facebook_metrics(log.meta_creation_id, _facebook_token_for_log(log), log.target.facebook_account.external_id if log.target.facebook_account else "")
                permalink = metrics.get("permalink_url", "")
            else:
                metrics = fetch_instagram_metrics(log.meta_creation_id, _instagram_token_for_log(log))
                permalink = metrics.get("permalink", "")
            rows.append(
                {
                    "source": "tool",
                    "target_id": str(log.target_id),
                    "target_name": log.target.display_name,
                    "sync_key": log.target.sync_key,
                    "platform": log.platform,
                    "post_id": log.meta_creation_id,
                    "published_at": timezone.localtime(log.published_at).isoformat() if log.published_at else "",
                    "permalink": permalink,
                    "drive_file_name": log.drive_file_name,
                    "metrics_json": json.dumps(metrics, ensure_ascii=False),
                    **{key: str(value) for key, value in metrics.items()},
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "source": "tool",
                    "target_id": str(log.target_id),
                    "target_name": log.target.display_name,
                    "sync_key": log.target.sync_key,
                    "platform": log.platform,
                    "post_id": log.meta_creation_id,
                    "published_at": timezone.localtime(log.published_at).isoformat() if log.published_at else "",
                    "permalink": "",
                    "drive_file_name": log.drive_file_name,
                    "error": str(exc),
                    "metrics_json": "{}",
                }
            )
    return rows


def load_manual_benchmark_rows(csv_path: str) -> list[dict]:
    path = Path(csv_path)
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append({key: (value or "").strip() for key, value in row.items()})
    return rows


def enrich_manual_benchmark_rows(rows: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        target = None
        if row.get("target_id"):
            target = PublishingTarget.objects.filter(pk=row["target_id"]).select_related(
                "credential",
                "facebook_account",
                "instagram_account",
            ).first()
        elif row.get("sync_key"):
            target = PublishingTarget.objects.filter(sync_key=row["sync_key"]).select_related(
                "credential",
                "facebook_account",
                "instagram_account",
            ).first()

        if target is None:
            enriched.append(
                {
                    **row,
                    "source": "manual",
                    "error": "Matching target not found.",
                    "metrics_json": "{}",
                }
            )
            continue

        post_id = row.get("post_id", "")
        platform = row.get("platform", "")
        try:
            if platform == SocialAccount.FACEBOOK:
                metrics = fetch_facebook_metrics(
                    post_id,
                    (target.facebook_account.access_token if target.facebook_account else "") or target.credential.access_token,
                    target.facebook_account.external_id if target.facebook_account else "",
                )
                permalink = metrics.get("permalink_url", "")
            else:
                metrics = fetch_instagram_metrics(
                    post_id,
                    ((target.instagram_account.access_token if target.instagram_account else "") or (target.facebook_account.access_token if target.facebook_account else "") or target.credential.access_token),
                )
                permalink = metrics.get("permalink", "")
            enriched.append(
                {
                    **row,
                    "source": "manual",
                    "target_name": target.display_name,
                    "target_id": str(target.pk),
                    "sync_key": target.sync_key,
                    "permalink": permalink,
                    "metrics_json": json.dumps(metrics, ensure_ascii=False),
                    **{key: str(value) for key, value in metrics.items()},
                }
            )
        except Exception as exc:
            enriched.append(
                {
                    **row,
                    "source": "manual",
                    "target_name": target.display_name,
                    "target_id": str(target.pk),
                    "sync_key": target.sync_key,
                    "error": str(exc),
                    "metrics_json": "{}",
                }
            )
    return enriched


def export_rows_to_csv(rows: list[dict], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
