from datetime import datetime, time
from io import BytesIO

from PIL import Image
from django.core.cache import cache
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from .forms import PublishingTargetForm
from .models import MetaCredential, PublishingTarget
from .services.diagnostics import build_rejection_diagnostics
from .services.ai import _build_model_candidates, _normalize_ai_payload, _payload_quality_errors, _resolve_model_name, build_ai_caption_for_media, get_or_generate_media_insight
from .services.drive import extract_drive_folder_id
from .services.health import build_target_health
from .services.media_transform import build_instagram_ready_image
from .services.publishing import _platform_already_succeeded_for_file, _publish_to_instagram, _slot_is_complete, build_caption, get_daily_slots, pick_next_shared_file, publish_due_targets
from .services.proxy import build_proxy_urls, sign_media_token, unsign_media_token
from .services.telegram import build_daily_report_message


class DriveHelpersTest(TestCase):
    def test_extract_drive_folder_id_from_url(self):
        folder_id = extract_drive_folder_id("https://drive.google.com/drive/folders/abc123XYZ?usp=sharing")
        self.assertEqual(folder_id, "abc123XYZ")

    def test_list_folder_files_handles_pagination(self):
        from unittest.mock import MagicMock, patch
        from .services.drive import list_folder_files

        service = MagicMock()
        files_resource = MagicMock()
        service.files.return_value = files_resource
        files_resource.list.return_value.execute.side_effect = [
            {"files": [{"id": "1", "name": "A", "mimeType": "image/jpeg"}], "nextPageToken": "page-2"},
            {"files": [{"id": "2", "name": "B", "mimeType": "video/mp4"}]},
        ]

        with patch("scheduler.services.drive.get_drive_service", return_value=service):
            result = list_folder_files("folder123")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "1")
        self.assertEqual(result[1]["id"], "2")


class SchedulingTest(TestCase):
    def test_daily_slots_count_matches_posts_per_day(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:1",
            display_name="Test Page",
            posts_per_day=3,
            posting_window_start=time(9, 0),
            posting_window_end=time(15, 0),
        )
        self.assertEqual(len(get_daily_slots(target)), 3)

    def test_explicit_posting_times_override_window(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:2",
            display_name="Explicit",
            posts_per_day=3,
            posting_times=["09:15", "12:30", "18:45"],
        )
        self.assertEqual([slot.strftime("%H:%M") for slot in get_daily_slots(target)], ["09:15", "12:30", "18:45"])

    def test_due_runner_moves_to_next_slot_only_after_current_slot_platforms_succeed(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:3|ig:3",
            display_name="Timed Pair",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            posts_per_day=2,
            posting_times=["09:00", "10:00"],
        )
        slots = get_daily_slots(target)
        target.post_logs.create(platform="facebook", scheduled_for=slots[0], status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
        target.post_logs.create(platform="instagram", scheduled_for=slots[0], status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
        self.assertTrue(_slot_is_complete(target, slots[0], {"facebook", "instagram"}))

        with patch("scheduler.services.publishing.publish_target") as publish_target_mock:
            publish_due_targets(reference_time=slots[1])
        publish_target_mock.assert_called_once_with(target, scheduled_for=slots[1])

    @override_settings(SCHEDULER_CATCHUP_MINUTES=60)
    def test_due_runner_ignores_old_missed_slots_outside_catchup_window(self):
        from unittest.mock import patch
        from django.utils import timezone

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb5", name="FB")
        ig = credential.accounts.create(platform="instagram", external_id="ig5", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:5|ig:5",
            display_name="No Backfill",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            posts_per_day=2,
            posting_times=["09:00", "10:00"],
        )
        reference_time = timezone.make_aware(datetime.strptime("2026-03-22 16:30", "%Y-%m-%d %H:%M"))

        with patch("scheduler.services.publishing.publish_target") as publish_target_mock:
            result = publish_due_targets(reference_time=reference_time)
        publish_target_mock.assert_not_called()
        self.assertEqual(result["success"], 0)


class ProxyHelpersTest(TestCase):
    @override_settings(PUBLIC_APP_BASE_URL="https://example.com")
    def test_build_proxy_url(self):
        urls = build_proxy_urls(16, "file123", "POST1.jpeg")
        self.assertEqual(len(urls), 2)
        self.assertIn("https://example.com/media-proxy/", urls[0])
        self.assertIn("POST1.jpeg", urls[0])

    def test_sign_and_unsign_media_token(self):
        token = sign_media_token(16, "file123")
        payload = unsign_media_token(token)
        self.assertEqual(payload["target_id"], 16)
        self.assertEqual(payload["file_id"], "file123")


class DiagnosticsTest(TestCase):
    def test_build_rejection_diagnostics_contains_possible_causes(self):
        message = build_rejection_diagnostics(
            "instagram",
            {"id": "1", "name": "POST4.mp4", "mimeType": "video/mp4"},
            "Instagram container failed with status ERROR.",
        )
        self.assertIn("Possible causes:", message)
        self.assertIn("video/mp4", message)


class AIServiceTest(TestCase):
    @override_settings(AI_API_BASE_URL="https://api.openai.com/v1")
    def test_openai_model_name_prefix_is_removed_for_openai_base_url(self):
        self.assertEqual(_resolve_model_name("openai/gpt-4.1-nano", "https://api.openai.com/v1"), "gpt-4.1-nano")
        self.assertEqual(_resolve_model_name("gpt-4.1-mini", "https://api.openai.com/v1"), "gpt-4.1-mini")

    @override_settings(
        AI_API_KEY="test-key",
        AI_API_BASE_URL="https://api.openai.com/v1",
        AI_MODEL="openai/gpt-4.1-nano",
    )
    def test_successful_ai_response_keeps_provider_metadata(self):
        from unittest.mock import MagicMock, patch

        from .services.ai import _call_openai_json

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"output_text": '{"primary_caption":"ok"}'}

        with patch("scheduler.services.ai.requests.post", return_value=success):
            payload = _call_openai_json("system", "user")

        self.assertEqual(payload["primary_caption"], "ok")
        self.assertEqual(payload["_ai_meta"]["requested_model"], "openai/gpt-4.1-nano")
        self.assertEqual(payload["_ai_meta"]["resolved_model"], "gpt-4.1-nano")
        self.assertEqual(payload["_ai_meta"]["provider_base_url"], "https://api.openai.com/v1")

    def test_normalize_ai_payload_cleans_list_and_text_shapes(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(credential=credential, sync_key="fb:norm", display_name="Norm")
        payload = _normalize_ai_payload(
            {
                "primary_caption": ["Line 1", "Line 2"],
                "hashtags": "#a #b",
                "quality_issues": "issue one, issue two",
                "secondary_tags": "tag1, tag2",
                "best_posting_times": "09:00, 18:00",
            },
            target,
            {"name": "POST1.jpeg"},
            ["10:00"],
            "fallback reason",
        )
        self.assertEqual(payload["primary_caption"], "Line 1\nLine 2")
        self.assertEqual(payload["hashtags"], ["#a", "#b"])
        self.assertEqual(payload["quality_issues"], ["issue one", "issue two"])
        self.assertEqual(payload["secondary_tags"], ["tag1", "tag2"])
        self.assertEqual(payload["best_posting_times"], ["09:00", "18:00"])

    def test_payload_quality_errors_flags_filename_like_and_sparse_output(self):
        errors = _payload_quality_errors(
            {
                "primary_caption": "POST92",
                "hashtags": "#one",
                "short_caption": "",
                "long_caption": "",
                "hindi_caption": "",
                "english_caption": "",
                "hinglish_caption": "",
                "translated_hindi": "",
                "translated_english": "",
                "translated_hinglish": "",
            },
            {"name": "POST92.mp4"},
        )
        self.assertIn("primary_caption looks like raw filename", errors)
        self.assertIn("not enough hashtags", errors)
        self.assertIn("too many rewrite/translation fields missing", errors)

    @override_settings(
        AI_API_KEY="test-openai-key",
        AI_API_BASE_URL="https://api.openai.com/v1",
        AI_MODEL="openai/gpt-4.1-nano",
        AI_FALLBACK_MODEL="openai/gpt-4.1-mini",
    )
    def test_build_model_candidates_supports_openai_fallback(self):
        candidates = _build_model_candidates()
        self.assertEqual(
            candidates,
            [
                {"model": "openai/gpt-4.1-nano", "base_url": "https://api.openai.com/v1", "api_key": "test-openai-key"},
                {"model": "openai/gpt-4.1-mini", "base_url": "https://api.openai.com/v1", "api_key": "test-openai-key"},
            ],
        )

    def test_ai_insight_falls_back_to_heuristics_without_api_key(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:ai1",
            display_name="AI Target",
            drive_folder_id="folder",
            default_caption="Base caption",
        )
        file_obj = {"id": "file1", "name": "Ayurveda Healing Tips 01.mp4", "mimeType": "video/mp4"}
        insight = get_or_generate_media_insight(target, file_obj=file_obj, force=True)
        self.assertEqual(insight.drive_file_id, "file1")
        self.assertEqual(insight.primary_caption, "Base caption")
        self.assertTrue(insight.best_posting_times)
        self.assertIn(insight.duplicate_risk, {"low", "medium", "high"})

    @override_settings(
        AI_API_KEY="test-openai-key",
        AI_API_BASE_URL="https://api.openai.com/v1",
        AI_MODEL="openai/gpt-4.1-nano",
        AI_FALLBACK_MODEL="openai/gpt-4.1-mini",
    )
    def test_ai_service_uses_openai_fallback_when_primary_model_fails(self):
        from unittest.mock import MagicMock, patch

        from .services.ai import _call_openai_json

        failed = MagicMock()
        failed.status_code = 400
        failed.json.return_value = {"error": {"message": "primary failed"}}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"output_text": '{"primary_caption":"ok"}'}

        with patch("scheduler.services.ai.requests.post", side_effect=[failed, success]) as post_mock:
            payload = _call_openai_json("system", "user")

        self.assertEqual(payload["primary_caption"], "ok")
        self.assertEqual(post_mock.call_args_list[0].kwargs["json"]["model"], "gpt-4.1-nano")
        self.assertEqual(post_mock.call_args_list[0].args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(post_mock.call_args_list[1].kwargs["json"]["model"], "gpt-4.1-mini")
        self.assertEqual(post_mock.call_args_list[1].args[0], "https://api.openai.com/v1/responses")

    @override_settings(
        AI_API_KEY="test-openai-key",
        AI_API_BASE_URL="https://api.openai.com/v1",
        AI_MODEL="openai/gpt-4.1-nano",
        AI_FALLBACK_MODEL="openai/gpt-4.1-mini",
    )
    def test_ai_payload_uses_openai_fallback_when_primary_output_is_weak(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:weak1",
            display_name="Weak Output",
            drive_folder_id="folder",
            ai_enabled=True,
        )
        weak_payload = {
            "primary_caption": "500+ Viral Health Awareness Reels by Digital Ceo Official92",
            "hashtags": "#one",
            "short_caption": "short",
            "long_caption": "",
            "hindi_caption": "",
            "english_caption": "",
            "hinglish_caption": "",
            "translated_hindi": "",
            "translated_english": "",
            "translated_hinglish": "",
            "_ai_meta": {
                "provider_base_url": "https://api.openai.com/v1",
                "requested_model": "openai/gpt-4.1-nano",
                "resolved_model": "gpt-4.1-nano",
            },
        }
        strong_payload = {
            "primary_caption": "Strong caption",
            "hashtags": ["#one", "#two", "#three"],
            "short_caption": "Short",
            "long_caption": "Long enough",
            "hindi_caption": "Hindi text",
            "english_caption": "English text",
            "hinglish_caption": "Hinglish text",
            "translated_hindi": "Hindi translation",
            "translated_english": "English translation",
            "translated_hinglish": "Hinglish translation",
            "primary_category": "wellness",
            "_ai_meta": {
                "provider_base_url": "https://api.openai.com/v1",
                "requested_model": "openai/gpt-4.1-mini",
                "resolved_model": "gpt-4.1-mini",
            },
        }

        with patch("scheduler.services.ai._call_openai_json", side_effect=[weak_payload, strong_payload]):
            insight = get_or_generate_media_insight(
                target,
                file_obj={"id": "file-weak", "name": "500+ Viral Health Awareness Reels by Digital Ceo Official92.mp4", "mimeType": "video/mp4"},
                force=True,
            )

        self.assertEqual(insight.primary_caption, "Strong caption")
        self.assertEqual(insight.raw_payload["_ai_meta"]["requested_model"], "openai/gpt-4.1-mini")

    @override_settings(AI_API_KEY="test-key")
    def test_ai_insight_populates_requested_feature_fields(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:ai2",
            display_name="AI Full",
            drive_folder_id="folder",
            default_caption="Base caption",
            ai_enabled=True,
        )
        file_obj = {"id": "file2", "name": "Womens Wellness Morning Tips.jpeg", "mimeType": "image/jpeg"}
        payload = {
            "primary_caption": "Primary caption",
            "hashtags": ["#wellness", "#morning"],
            "short_caption": "Short version",
            "long_caption": "Long version",
            "hindi_caption": "Hindi rewrite",
            "english_caption": "English rewrite",
            "hinglish_caption": "Hinglish rewrite",
            "primary_category": "women wellness",
            "secondary_tags": ["health", "routine"],
            "duplicate_risk": "low",
            "duplicate_reason": "Looks fresh.",
            "quality_risk": "low",
            "quality_issues": [],
            "safe_to_post": True,
            "translated_hindi": "Hindi translation",
            "translated_english": "English translation",
            "translated_hinglish": "Hinglish translation",
            "best_posting_times": ["09:00", "18:00"],
            "best_posting_reason": "Morning and evening performed best.",
            "report_summary": "Smart summary",
        }

        with patch("scheduler.services.ai._call_openai_json", return_value=payload):
            insight = get_or_generate_media_insight(target, file_obj=file_obj, force=True)

        self.assertEqual(insight.primary_caption, "Primary caption")
        self.assertEqual(insight.hashtags, ["#wellness", "#morning"])
        self.assertEqual(insight.short_caption, "Short version")
        self.assertEqual(insight.long_caption, "Long version")
        self.assertEqual(insight.hindi_caption, "Hindi rewrite")
        self.assertEqual(insight.english_caption, "English rewrite")
        self.assertEqual(insight.hinglish_caption, "Hinglish rewrite")
        self.assertEqual(insight.translated_hindi, "Hindi translation")
        self.assertEqual(insight.translated_english, "English translation")
        self.assertEqual(insight.translated_hinglish, "Hinglish translation")
        self.assertEqual(insight.duplicate_risk, "low")
        self.assertEqual(insight.quality_risk, "low")
        self.assertEqual(insight.primary_category, "women wellness")
        self.assertEqual(insight.best_posting_times, ["09:00", "18:00"])
        self.assertEqual(target.ai_last_report_summary, "Smart summary")

    @override_settings(AI_API_KEY="test-key")
    def test_build_ai_caption_for_media_uses_primary_caption_and_hashtags(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:ai3",
            display_name="AI Caption",
            drive_folder_id="folder",
            ai_enabled=True,
        )
        file_obj = {"id": "file3", "name": "Healing.jpeg", "mimeType": "image/jpeg"}
        payload = {
            "primary_caption": "Generated caption",
            "hashtags": ["#a", "#b"],
            "short_caption": "",
            "long_caption": "",
            "hindi_caption": "",
            "english_caption": "",
            "hinglish_caption": "",
            "primary_category": "general",
            "secondary_tags": [],
            "duplicate_risk": "low",
            "duplicate_reason": "",
            "quality_risk": "low",
            "quality_issues": [],
            "safe_to_post": True,
            "translated_hindi": "",
            "translated_english": "",
            "translated_hinglish": "",
            "best_posting_times": ["10:00"],
            "best_posting_reason": "",
            "report_summary": "",
        }

        with patch("scheduler.services.ai._call_openai_json", return_value=payload):
            caption = build_ai_caption_for_media(target, file_obj)

        self.assertEqual(caption, "Generated caption\n\n#a #b")

    def test_auto_caption_toggle_uses_ai_caption_on_publish_path(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:ai4",
            display_name="AI Auto",
            drive_folder_id="folder",
            default_caption="Default",
            ai_enabled=True,
            ai_auto_caption_enabled=True,
        )
        file_obj = {"id": "file4", "name": "Healing.jpeg", "mimeType": "image/jpeg"}

        with patch("scheduler.services.publishing.build_ai_caption_for_media", return_value="AI caption"):
            caption = build_caption(target, file_obj=file_obj)

        self.assertEqual(caption, "AI caption")

    @override_settings(AI_API_KEY="test-key")
    def test_daily_report_message_includes_ai_summary(self):
        from unittest.mock import patch
        from django.utils import timezone

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(credential=credential, sync_key="fb:ai5", display_name="AI Report")
        target.post_logs.create(
            platform="facebook",
            scheduled_for=timezone.now(),
            published_at=timezone.now(),
            status="success",
            drive_file_id="file5",
            drive_file_name="POST5.jpeg",
            message="ok",
        )

        with patch("scheduler.services.telegram.build_ai_report_summary", return_value="AI says things look good."):
            message = build_daily_report_message(timezone.localdate())

        self.assertIn("AI SUMMARY", message)
        self.assertIn("AI says things look good.", message)

    def test_daily_report_message_uses_requested_target_status_layout(self):
        from django.utils import timezone

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(credential=credential, sync_key="fb:report1", display_name="Page name")
        target.post_logs.create(
            platform="facebook",
            scheduled_for=timezone.now(),
            published_at=timezone.now(),
            status="success",
            drive_file_id="file-fb",
            drive_file_name="POST1.jpeg",
        )
        target.post_logs.create(
            platform="facebook",
            scheduled_for=timezone.now(),
            published_at=timezone.now(),
            status="success",
            drive_file_id="file-fb-2",
            drive_file_name="POST2.jpeg",
        )
        target.post_logs.create(
            platform="instagram",
            scheduled_for=timezone.now(),
            status="failed",
            drive_file_id="file-ig",
            drive_file_name="POST1.jpeg",
            message="Media ID is not available",
        )

        message = build_daily_report_message(timezone.localdate())

        self.assertIn("ACTIVITY DETAILS", message)
        self.assertIn("TARGET 1", message)
        self.assertIn("Page name", message)
        self.assertIn("- Facebook: 2 successful posts", message)
        self.assertIn("- Instagram: 0 successful posts, 1 failed attempt", message)
        self.assertIn("Published at:", message)
        self.assertIn("Last failed at:", message)
        self.assertIn("Last issue: Media ID is not available", message)
        self.assertNotIn("NEEDS ATTENTION", message)


class AIViewFlowTest(TestCase):
    @override_settings(AI_API_KEY="test-key")
    def test_generate_insight_and_apply_caption_buttons_work(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:view1",
            display_name="AI View",
            drive_folder_id="folder",
            ai_enabled=True,
        )
        file_obj = {"id": "file6", "name": "Healthy Morning.jpeg", "mimeType": "image/jpeg"}
        payload = {
            "primary_caption": "Primary caption from AI",
            "hashtags": ["#fit", "#fresh"],
            "short_caption": "Short",
            "long_caption": "Long",
            "hindi_caption": "Hindi",
            "english_caption": "English",
            "hinglish_caption": "Hinglish",
            "primary_category": "wellness",
            "secondary_tags": ["tag1"],
            "duplicate_risk": "low",
            "duplicate_reason": "Fresh",
            "quality_risk": "low",
            "quality_issues": [],
            "safe_to_post": True,
            "translated_hindi": "Hindi translation",
            "translated_english": "English translation",
            "translated_hinglish": "Hinglish translation",
            "best_posting_times": ["09:00"],
            "best_posting_reason": "Best slot",
            "report_summary": "Summary",
        }

        with patch("scheduler.services.ai._next_candidate_file", return_value=file_obj), patch(
            "scheduler.services.ai._call_openai_json", return_value=payload
        ):
            response = self.client.post(
                reverse("scheduler:target_detail", args=[target.pk]),
                {"action": "generate_ai_insight"},
                follow=True,
            )
            self.assertContains(response, "AI insight generated for Healthy Morning.jpeg.")
            response = self.client.post(
                reverse("scheduler:target_detail", args=[target.pk]),
                {"action": "apply_ai_caption"},
                follow=True,
            )

        target.refresh_from_db()
        self.assertContains(response, "AI caption applied from Healthy Morning.jpeg.")
        self.assertEqual(target.default_caption, "Primary caption from AI\n\n#fit #fresh")

    def test_target_detail_renders_ai_meta_without_template_error(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:view2",
            display_name="AI Meta View",
            drive_folder_id="folder",
        )
        target.ai_media_insights.create(
            drive_file_id="file-meta",
            drive_file_name="POST1.jpeg",
            source_mime_type="image/jpeg",
            primary_caption="Caption",
            raw_payload={
                "_ai_meta": {
                    "requested_model": "openai/gpt-4.1-mini",
                    "resolved_model": "gpt-4.1-mini",
                    "provider_base_url": "https://api.openai.com/v1",
                }
            },
        )

        response = self.client.get(reverse("scheduler:target_detail", args=[target.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "openai/gpt-4.1-mini")

    def test_test_post_starts_in_background_and_redirects(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:view3",
            display_name="Async Test Post",
            drive_folder_id="folder",
        )

        with patch("scheduler.views.threading.Thread") as thread_mock:
            response = self.client.post(
                reverse("scheduler:target_detail", args=[target.pk]),
                {"action": "test_post"},
                follow=True,
            )

        target.refresh_from_db()
        self.assertEqual(target.last_status, "running")
        self.assertEqual(target.last_error, "")
        thread_mock.assert_called_once()
        self.assertContains(response, "Test post started in background.")


class SharedQueueTest(TestCase):
    def test_same_file_is_retained_until_all_platforms_succeed(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:1|ig:1",
            display_name="Pair",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )

        from unittest.mock import patch
        files = [
            {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg"},
            {"id": "file2", "name": "POST2.mp4", "mimeType": "video/mp4"},
        ]
        with patch("scheduler.services.publishing.list_folder_files", return_value=files):
            self.assertEqual(pick_next_shared_file(target)["id"], "file1")
            target.post_logs.create(platform="facebook", scheduled_for=get_daily_slots(target)[0], status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
            self.assertTrue(_platform_already_succeeded_for_file(target, "facebook", "file1"))
            self.assertEqual(pick_next_shared_file(target)["id"], "file1")
            target.post_logs.create(platform="instagram", scheduled_for=get_daily_slots(target)[0], status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
            self.assertEqual(pick_next_shared_file(target)["id"], "file2")

    def test_fully_published_media_is_not_reused(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:9|ig:9",
            display_name="No Reuse",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )

        from unittest.mock import patch

        files = [
            {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg"},
        ]
        slot = get_daily_slots(target)[0]
        target.post_logs.create(platform="facebook", scheduled_for=slot, status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
        target.post_logs.create(platform="instagram", scheduled_for=slot, status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")

        with patch("scheduler.services.publishing.list_folder_files", return_value=files):
            with self.assertRaisesMessage(Exception, "already been published on every active platform"):
                pick_next_shared_file(target)


class HealthTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_health_includes_cached_asset_count(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(credential=credential, sync_key="fb:1", display_name="T")
        target.media_assets.create(
            drive_file_id="1",
            drive_file_name="POST1.jpeg",
            variant="default",
            public_filename="POST1.jpeg",
            status="ready",
        )
        health = build_target_health(target)
        self.assertEqual(health["cached_asset_count"], 1)

    def test_health_reuses_cached_drive_summary(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(credential=credential, sync_key="fb:cache", display_name="Cached", drive_folder_id="folder")
        files = [{"id": "1", "name": "POST1.jpeg", "mimeType": "image/jpeg"}]

        with patch("scheduler.services.health.list_folder_files", return_value=files) as list_mock:
            first = build_target_health(target)
            second = build_target_health(target)

        self.assertEqual(first["file_count"], 1)
        self.assertEqual(second["file_count"], 1)
        list_mock.assert_called_once_with("folder")


class MediaTransformTest(TestCase):
    def test_instagram_ready_image_returns_small_jpeg(self):
        image = Image.new("RGBA", (2200, 2200), color=(255, 0, 0, 128))
        source = BytesIO()
        image.save(source, format="PNG")

        output = build_instagram_ready_image(source.getvalue())

        self.assertLessEqual(len(output), 8 * 1024 * 1024)
        converted = Image.open(BytesIO(output))
        self.assertEqual(converted.format, "JPEG")
        self.assertLessEqual(max(converted.size), 1440)


class InstagramPublishTest(TestCase):
    def test_instagram_image_waits_for_container_before_publish(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:wait",
            display_name="IG Wait",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file1", "name": "POST15.jpeg", "mimeType": "image/jpeg"}

        with patch("scheduler.services.publishing.get_cached_public_urls", return_value=["https://example.com/POST15.jpg"]), patch(
            "scheduler.services.publishing._graph_post",
            side_effect=[{"id": "container-1"}, {"id": "publish-1"}],
        ) as graph_post_mock, patch("scheduler.services.publishing._wait_for_instagram_container") as wait_mock:
            result = _publish_to_instagram(target, file_obj)

        self.assertEqual(result, "publish-1")
        wait_mock.assert_called_once_with("container-1", "page-token")
        self.assertEqual(graph_post_mock.call_count, 2)


class PostingTimesFormTest(TestCase):
    def test_form_requires_one_time_per_post(self):
        form = PublishingTargetForm(
            data={
                "drive_folder_url": "",
                "drive_folder_id": "",
                "posts_per_day": 3,
                "posting_times_json": '["09:00","12:00","18:00"]',
                "posting_window_start": "10:00",
                "posting_window_end": "18:00",
                "default_caption": "",
                "is_active": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["posting_times"], ["09:00", "12:00", "18:00"])
