from datetime import time

from django.test import TestCase
from django.test.utils import override_settings

from .forms import PublishingTargetForm
from .models import MetaCredential, PublishingTarget
from .services.diagnostics import build_rejection_diagnostics
from .services.drive import extract_drive_folder_id
from .services.health import build_target_health
from .services.publishing import get_daily_slots, pick_next_shared_file
from .services.proxy import build_proxy_urls, sign_media_token, unsign_media_token


class DriveHelpersTest(TestCase):
    def test_extract_drive_folder_id_from_url(self):
        folder_id = extract_drive_folder_id("https://drive.google.com/drive/folders/abc123XYZ?usp=sharing")
        self.assertEqual(folder_id, "abc123XYZ")


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
            self.assertEqual(pick_next_shared_file(target)["id"], "file1")
            target.post_logs.create(platform="instagram", scheduled_for=get_daily_slots(target)[0], status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
            self.assertEqual(pick_next_shared_file(target)["id"], "file2")


class HealthTest(TestCase):
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
