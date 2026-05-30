import base64
from datetime import datetime, time, timedelta
from io import BytesIO, StringIO
from pathlib import Path
import tempfile

from PIL import Image
from django.core.management import call_command
from django.core.cache import cache
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import PublishingTargetForm
from .models import MediaAsset, MetaCredential, MetaCredentialEvent, PostLog, PublishingTarget, ScheduledPostRun, SocialAccount
from .services.diagnostics import build_rejection_diagnostics
from .services.ai import _build_model_candidates, _clean_media_name_context, _normalize_ai_payload, _payload_quality_errors, _resolve_model_name, build_ai_caption_for_media, get_or_generate_media_insight
from .services.compliance import evaluate_publish_readiness
from .services.drive import extract_drive_folder_id
from .services.health import build_target_health
from .services.metrics import fetch_facebook_metrics, iter_tool_post_metrics
from .services.media_transform import build_instagram_ready_image
from .services.publishing import PublishBackoff, PublishingError, _platform_already_succeeded_for_file, _publish_to_facebook, _publish_to_instagram, _slot_is_complete, _wait_for_instagram_container, build_caption, get_daily_slots, pick_next_shared_file, process_scheduled_run, publish_due_targets, publish_platform
from .services.proxy import build_proxy_urls, sign_media_token, unsign_media_token
from .services.telegram import TELEGRAM_MESSAGE_MAX_LENGTH, _split_telegram_message, build_daily_report_message


class DriveHelpersTest(TestCase):
    def test_extract_drive_folder_id_from_url(self):
        folder_id = extract_drive_folder_id("https://drive.google.com/drive/folders/abc123XYZ?usp=sharing")
        self.assertEqual(folder_id, "abc123XYZ")

    def test_list_folder_files_handles_pagination(self):
        from unittest.mock import MagicMock, patch
        from .services.drive import DRIVE_LIST_FILE_FIELDS, list_folder_files

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
        fields_arg = files_resource.list.call_args.kwargs["fields"]
        self.assertEqual(fields_arg, f"nextPageToken,files({DRIVE_LIST_FILE_FIELDS})")

    def test_get_drive_file_metadata_requests_cache_fingerprint_fields(self):
        from unittest.mock import MagicMock, patch
        from .services.drive import DRIVE_FILE_FIELDS, get_drive_file_metadata

        service = MagicMock()
        files_resource = MagicMock()
        service.files.return_value = files_resource
        files_resource.get.return_value.execute.return_value = {"id": "file1"}

        with patch("scheduler.services.drive.get_drive_service", return_value=service):
            get_drive_file_metadata("file1")

        fields_arg = files_resource.get.call_args.kwargs["fields"]
        self.assertEqual(fields_arg, DRIVE_FILE_FIELDS)


class MetaAPIClientTest(TestCase):
    def test_graph_get_reports_non_json_response_cleanly(self):
        from unittest.mock import MagicMock, patch
        from .services.meta import MetaAPIError, _graph_get

        response = MagicMock()
        response.status_code = 502
        response.text = "<html>bad gateway</html>"
        response.json.side_effect = ValueError("No JSON")

        with patch("scheduler.services.meta.requests.get", return_value=response):
            with self.assertRaisesMessage(MetaAPIError, "non-JSON response"):
                _graph_get("/me", "token")

    def test_graph_get_reports_meta_error_details(self):
        from unittest.mock import MagicMock, patch
        from .services.meta import MetaAPIError, _graph_get

        response = MagicMock()
        response.status_code = 400
        response.text = '{"error": "..."}'
        response.json.return_value = {
            "error": {
                "message": "Application request limit reached",
                "type": "OAuthException",
                "code": 4,
                "error_subcode": 2207008,
                "fbtrace_id": "TRACE123",
            }
        }

        with patch("scheduler.services.meta.requests.get", return_value=response):
            with self.assertRaises(MetaAPIError) as ctx:
                _graph_get("/me", "token")

        message = str(ctx.exception)
        self.assertIn("Application request limit reached", message)
        self.assertIn("type=OAuthException", message)
        self.assertIn("code=4", message)
        self.assertIn("subcode=2207008", message)
        self.assertIn("trace=TRACE123", message)


class MetaSyncPreservationTest(TestCase):
    def _asset_bundle(self, pages=None, instagram_accounts=None, me=None):
        from .services.meta import AssetBundle

        return AssetBundle(
            pages=pages or [],
            instagram_accounts=instagram_accounts or [],
            me=me or {"id": "user-new", "name": "New Meta User"},
        )

    def test_sync_skips_existing_pair_under_new_credential_and_preserves_settings(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        old_credential = MetaCredential.objects.create(label="Old", access_token="old-token")
        old_fb = old_credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-1", name="Old FB")
        old_ig = old_credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-1", name="old_ig")
        target = PublishingTarget.objects.create(
            credential=old_credential,
            sync_key="fb:fb-1|ig:ig-1",
            display_name="Old Pair",
            facebook_account=old_fb,
            instagram_account=old_ig,
            drive_folder_id="drive-folder",
            drive_folder_url="https://drive.google.com/drive/folders/drive-folder",
            posts_per_day=3,
            posting_times=["09:00", "12:00", "18:00"],
            default_caption="Keep this caption",
            ai_enabled=True,
            ai_auto_caption_enabled=True,
            ai_language="Hindi",
            ai_tone="Warm",
            last_status="success",
        )
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=timezone.make_aware(datetime(2026, 3, 22, 9, 0)),
            status="success",
            drive_file_id="file-1",
            drive_file_name="POST1.jpeg",
        )

        new_credential = MetaCredential.objects.create(label="New", access_token="new-token")
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-1",
                    "name": "New FB Name",
                    "access_token": "new-page-token",
                    "instagram_business_account": {"id": "ig-1", "username": "new_ig", "name": "New IG Name"},
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(new_credential)

        target.refresh_from_db()
        self.assertEqual(PublishingTarget.objects.count(), 1)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["reused"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(target.credential, old_credential)
        self.assertEqual(target.drive_folder_id, "drive-folder")
        self.assertEqual(target.drive_folder_url, "https://drive.google.com/drive/folders/drive-folder")
        self.assertEqual(target.posts_per_day, 3)
        self.assertEqual(target.posting_times, ["09:00", "12:00", "18:00"])
        self.assertEqual(target.default_caption, "Keep this caption")
        self.assertTrue(target.ai_enabled)
        self.assertTrue(target.ai_auto_caption_enabled)
        self.assertEqual(target.ai_language, "Hindi")
        self.assertEqual(target.ai_tone, "Warm")
        self.assertTrue(target.is_active)
        self.assertEqual(target.last_status, "success")
        self.assertEqual(target.post_logs.count(), 1)
        self.assertEqual(target.facebook_account.access_token, "")
        self.assertEqual(target.instagram_account.external_id, "ig-1")

    def test_sync_upgrades_existing_facebook_only_target_to_pair_without_losing_settings(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        credential = MetaCredential.objects.create(label="Token", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-2", name="FB")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-2",
            display_name="FB Only",
            facebook_account=fb,
            drive_folder_id="folder-2",
            posts_per_day=2,
            posting_times=["10:00", "17:00"],
            default_caption="Still here",
        )
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-2",
                    "name": "FB",
                    "access_token": "page-token",
                    "instagram_business_account": {"id": "ig-2", "username": "ig_two", "name": "IG Two"},
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            sync_credential_accounts(credential)

        target.refresh_from_db()
        self.assertEqual(PublishingTarget.objects.count(), 1)
        self.assertEqual(target.sync_key, "fb:fb-2|ig:ig-2")
        self.assertEqual(target.drive_folder_id, "folder-2")
        self.assertEqual(target.posting_times, ["10:00", "17:00"])
        self.assertEqual(target.default_caption, "Still here")
        self.assertEqual(target.instagram_account.external_id, "ig-2")

    def test_sync_does_not_create_duplicate_for_same_pair_under_new_credential(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        old_credential = MetaCredential.objects.create(label="Old", access_token="old-token")
        old_fb = old_credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-3", name="FB")
        old_ig = old_credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-3", name="IG")
        PublishingTarget.objects.create(
            credential=old_credential,
            sync_key="fb:fb-3|ig:ig-3",
            display_name="Configured Pair",
            facebook_account=old_fb,
            instagram_account=old_ig,
            drive_folder_id="folder-3",
        )
        new_credential = MetaCredential.objects.create(label="New", access_token="new-token")
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-3",
                    "name": "FB",
                    "access_token": "page-token",
                    "instagram_business_account": {"id": "ig-3", "username": "ig_three", "name": "IG"},
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(new_credential)

        self.assertEqual(PublishingTarget.objects.count(), 1)
        target = PublishingTarget.objects.get()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["reused"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(target.credential, old_credential)
        self.assertEqual(target.drive_folder_id, "folder-3")

    def test_sync_second_token_with_new_page_adds_target_without_touching_existing_targets(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        old_credential = MetaCredential.objects.create(label="Old Token", access_token="old-token")
        old_fb = old_credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-old", name="Old Page")
        old_target = PublishingTarget.objects.create(
            credential=old_credential,
            sync_key="fb:fb-old",
            display_name="Old Page",
            facebook_account=old_fb,
            drive_folder_id="old-folder",
            posting_times=["09:00", "18:00"],
            posts_per_day=2,
        )
        new_credential = MetaCredential.objects.create(label="New Token", access_token="new-token")
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-new-token",
                    "name": "New Token Page",
                    "access_token": "new-page-token",
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(new_credential)

        old_target.refresh_from_db()
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["reused"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(PublishingTarget.objects.count(), 2)
        self.assertEqual(old_target.credential, old_credential)
        self.assertEqual(old_target.drive_folder_id, "old-folder")
        self.assertTrue(PublishingTarget.objects.filter(credential=new_credential, sync_key="fb:fb-new-token").exists())

    def test_sync_second_token_with_existing_facebook_only_page_is_skipped(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        old_credential = MetaCredential.objects.create(label="Old", access_token="old-token")
        old_fb = old_credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-only", name="FB Only")
        target = PublishingTarget.objects.create(
            credential=old_credential,
            sync_key="fb:fb-only",
            display_name="FB Only",
            facebook_account=old_fb,
            drive_folder_id="configured-folder",
        )
        new_credential = MetaCredential.objects.create(label="New", access_token="new-token")
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-only",
                    "name": "FB Only From New Token",
                    "access_token": "new-page-token",
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(new_credential)

        target.refresh_from_db()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["reused"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(PublishingTarget.objects.count(), 1)
        self.assertEqual(target.credential, old_credential)
        self.assertEqual(target.drive_folder_id, "configured-folder")

    def test_sync_second_token_with_new_instagram_only_account_adds_target(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        old_credential = MetaCredential.objects.create(label="Old Token", access_token="old-token")
        old_ig = old_credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-old", name="Old IG")
        old_target = PublishingTarget.objects.create(
            credential=old_credential,
            sync_key="ig:ig-old",
            display_name="Old IG",
            instagram_account=old_ig,
            drive_folder_id="old-folder",
        )
        new_credential = MetaCredential.objects.create(label="New Token", access_token="new-token")
        assets = self._asset_bundle(
            instagram_accounts=[{"id": "ig-new-business", "username": "new_business", "name": "New Business IG"}]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(new_credential)

        old_target.refresh_from_db()
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["reused"], 0)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(PublishingTarget.objects.count(), 2)
        self.assertEqual(old_target.credential, old_credential)
        self.assertEqual(old_target.drive_folder_id, "old-folder")
        self.assertTrue(PublishingTarget.objects.filter(credential=new_credential, sync_key="ig:ig-new-business").exists())

    def test_sync_second_token_with_existing_instagram_only_account_is_skipped(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        old_credential = MetaCredential.objects.create(label="Old", access_token="old-token")
        old_ig = old_credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-only", name="IG Only")
        target = PublishingTarget.objects.create(
            credential=old_credential,
            sync_key="ig:ig-only",
            display_name="IG Only",
            instagram_account=old_ig,
            drive_folder_id="configured-folder",
        )
        new_credential = MetaCredential.objects.create(label="New", access_token="new-token")
        assets = self._asset_bundle(
            instagram_accounts=[{"id": "ig-only", "username": "ig_only", "name": "IG Only From New Token"}]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(new_credential)

        target.refresh_from_db()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["reused"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(PublishingTarget.objects.count(), 1)
        self.assertEqual(target.credential, old_credential)
        self.assertEqual(target.drive_folder_id, "configured-folder")

    def test_fetch_meta_assets_includes_business_manager_instagram_accounts(self):
        from unittest.mock import patch
        from .services.meta import fetch_meta_assets

        def fake_graph_get(path, _token, params=None):
            fields = (params or {}).get("fields", "")
            if path == "/me" and fields == "id,name":
                return {"id": "user-1", "name": "User One"}
            if path == "/me/accounts":
                return {"data": []}
            if path == "/me" and fields.startswith("accounts{"):
                return {"accounts": {"data": []}}
            if path == "/me" and fields == "instagram_accounts{id,username,name}":
                return {"instagram_accounts": []}
            if path == "/me" and fields.startswith("businesses{"):
                return {
                    "businesses": {
                        "data": [
                            {
                                "instagram_accounts": {"data": [{"id": "ig-business", "username": "biz", "name": "Business IG"}]},
                                "owned_instagram_accounts": {"data": [{"id": "ig-owned", "username": "owned", "name": "Owned IG"}]},
                            }
                        ]
                    }
                }
            return {}

        with patch("scheduler.services.meta._graph_get", side_effect=fake_graph_get):
            assets = fetch_meta_assets("token")

        self.assertEqual({item["id"] for item in assets.instagram_accounts}, {"ig-business", "ig-owned"})

    def test_sync_retires_empty_duplicate_when_configured_target_becomes_pair(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        credential = MetaCredential.objects.create(label="Token", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-dupe", name="FB")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-dupe", name="IG")
        configured = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-dupe",
            display_name="Configured FB",
            facebook_account=fb,
            drive_folder_id="configured-folder",
            posting_times=["09:00", "18:00"],
            posts_per_day=2,
        )
        empty_duplicate = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-dupe|ig:ig-dupe",
            display_name="Empty Pair",
            facebook_account=fb,
            instagram_account=ig,
        )
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-dupe",
                    "name": "FB",
                    "access_token": "page-token",
                    "instagram_business_account": {"id": "ig-dupe", "username": "ig_dupe", "name": "IG"},
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            sync_credential_accounts(credential)

        configured.refresh_from_db()
        empty_duplicate.refresh_from_db()
        self.assertEqual(configured.sync_key, "fb:fb-dupe|ig:ig-dupe")
        self.assertEqual(configured.drive_folder_id, "configured-folder")
        self.assertEqual(configured.posting_times, ["09:00", "18:00"])
        self.assertTrue(configured.is_active)
        self.assertFalse(empty_duplicate.is_active)
        self.assertTrue(empty_duplicate.sync_key.startswith("archived:"))
        self.assertIn("Merged into target", empty_duplicate.last_error)
        self.assertEqual(PublishingTarget.objects.filter(is_active=True).count(), 1)

    def test_sync_missing_response_does_not_deactivate_configured_target(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        credential = MetaCredential.objects.create(label="Token", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-4", name="FB")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-4",
            display_name="Configured",
            facebook_account=fb,
            drive_folder_id="folder-4",
            posts_per_day=4,
            posting_times=["09:00", "11:00", "15:00", "19:00"],
            is_active=True,
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=self._asset_bundle()):
            result = sync_credential_accounts(credential)

        target.refresh_from_db()
        self.assertTrue(target.is_active)
        self.assertEqual(target.drive_folder_id, "folder-4")
        self.assertEqual(target.posting_times, ["09:00", "11:00", "15:00", "19:00"])
        self.assertIn("not returned", target.last_error)
        self.assertEqual(result["missing"], 1)

    def test_sync_creates_new_default_target_for_new_page(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        credential = MetaCredential.objects.create(label="Token", access_token="token")
        assets = self._asset_bundle(
            pages=[
                {
                    "id": "fb-new",
                    "name": "Fresh Page",
                    "access_token": "page-token",
                }
            ]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            result = sync_credential_accounts(credential)

        target = PublishingTarget.objects.get()
        self.assertEqual(result["created"], 1)
        self.assertEqual(target.sync_key, "fb:fb-new")
        self.assertEqual(target.display_name, "Fresh Page")
        self.assertEqual(target.drive_folder_id, "")
        self.assertEqual(target.posts_per_day, 1)

    def test_sync_does_not_clear_existing_publish_failure_context(self):
        from unittest.mock import patch
        from .services.meta import sync_credential_accounts

        credential = MetaCredential.objects.create(label="Token", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-keep-error", name="FB")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-keep-error",
            display_name="Keep Error",
            facebook_account=fb,
            last_status="failed",
            last_error="instagram: Meta transient backoff active for this target/platform/file.",
        )
        assets = self._asset_bundle(
            pages=[{"id": "fb-keep-error", "name": "FB", "access_token": "page-token"}]
        )

        with patch("scheduler.services.meta.fetch_meta_assets", return_value=assets):
            sync_credential_accounts(credential)

        target.refresh_from_db()
        self.assertEqual(target.last_status, "failed")
        self.assertIn("Meta transient backoff", target.last_error)


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

        with patch("scheduler.services.publishing.process_scheduled_run", return_value="success") as process_mock:
            publish_due_targets(reference_time=slots[1])
        process_mock.assert_called_once()
        self.assertEqual(timezone.localtime(process_mock.call_args.args[0].scheduled_for), slots[1])

    @override_settings(SCHEDULER_CATCHUP_MINUTES=60, SCHEDULER_BACKLOG_DAYS=2)
    def test_due_runner_recovers_old_missed_slots_inside_backlog_window(self):
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

        with patch("scheduler.services.publishing.process_scheduled_run", return_value="success") as process_mock:
            result = publish_due_targets(reference_time=reference_time)
        process_mock.assert_called_once()
        self.assertEqual(timezone.localtime(process_mock.call_args.args[0].scheduled_for).strftime("%H:%M"), "09:00")
        self.assertEqual(result["success"], 1)
        self.assertTrue(target.scheduled_runs.filter(scheduled_for__hour=9, status=ScheduledPostRun.STATUS_RUNNING).exists())

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90)
    def test_due_runner_counts_backoff_as_skipped_without_duplicate_log(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-backoff", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:due-backoff",
            display_name="Due Backoff",
            instagram_account=ig,
            drive_folder_id="folder",
            posting_times=["09:00"],
        )
        file_obj = {"id": "file-backoff", "name": "POST1.mp4", "mimeType": "video/mp4"}
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.make_aware(datetime(2026, 3, 22, 9, 0)),
            status=PostLog.STATUS_FAILED,
            drive_file_id=file_obj["id"],
            drive_file_name=file_obj["name"],
            message="Instagram publish failed: Authorization Error",
        )

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]), patch(
            "scheduler.services.publishing._publish_to_instagram"
        ) as publish_mock:
            result = publish_due_targets(reference_time=timezone.make_aware(datetime(2026, 3, 22, 9, 30)))

        publish_mock.assert_not_called()
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["backoff"], 1)
        self.assertEqual(target.post_logs.count(), 1)
        run = target.scheduled_runs.get()
        self.assertEqual(run.status, ScheduledPostRun.STATUS_BACKOFF)
        self.assertIn("Meta transient backoff", run.last_error)
        target.refresh_from_db()
        self.assertEqual(target.last_status, "backoff")

    def test_due_runner_counts_content_exhausted_as_skipped(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-exhausted", name="FB", access_token="page-token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:exhausted",
            display_name="Exhausted",
            facebook_account=fb,
            drive_folder_id="folder",
            posting_times=["09:00"],
        )
        file_obj = {"id": "file-done", "name": "POST1.jpeg", "mimeType": "image/jpeg"}
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=timezone.make_aware(datetime(2026, 3, 21, 9, 0)),
            status=PostLog.STATUS_SUCCESS,
            drive_file_id=file_obj["id"],
            drive_file_name=file_obj["name"],
        )

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]):
            result = publish_due_targets(reference_time=timezone.make_aware(datetime(2026, 3, 22, 9, 30)))

        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["content_exhausted"], 1)
        self.assertEqual(target.scheduled_runs.get().status, ScheduledPostRun.STATUS_SKIPPED)
        target.refresh_from_db()
        self.assertEqual(target.last_status, "content_exhausted")

    def test_due_runner_ignores_targets_under_archived_credentials(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Archived", access_token="token", is_active=False)
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-archived", name="FB", access_token="page-token")
        PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:archived",
            display_name="Archived Target",
            facebook_account=fb,
            drive_folder_id="folder",
            posting_times=["09:00"],
            is_active=True,
        )

        with patch("scheduler.services.publishing.process_scheduled_run") as process_mock:
            result = publish_due_targets(reference_time=timezone.make_aware(datetime(2026, 3, 22, 9, 30)))

        process_mock.assert_not_called()
        self.assertEqual(result["checked_targets"], 0)
        self.assertEqual(result["processed_runs"], 0)

    @override_settings(SCHEDULER_MAX_RUNS_PER_TICK=1)
    def test_due_runner_does_not_spend_run_cap_on_blank_drive_targets(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb_missing = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-missing", name="FB Missing", access_token="page-token")
        PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:missing-drive",
            display_name="A Missing Drive",
            facebook_account=fb_missing,
            posting_times=["09:00"],
        )
        fb_ready = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-ready", name="FB Ready", access_token="page-token")
        ready_target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:ready-after-missing",
            display_name="Z Ready Target",
            facebook_account=fb_ready,
            drive_folder_id="folder",
            posting_times=["09:00"],
        )
        file_obj = {"id": "file-ready", "name": "POST1.jpeg", "mimeType": "image/jpeg"}

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]), patch(
            "scheduler.services.publishing._publish_to_facebook",
            return_value="fb-post",
        ) as publish_mock:
            result = publish_due_targets(reference_time=timezone.make_aware(datetime(2026, 3, 22, 9, 30)))

        publish_mock.assert_called_once()
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["processed_runs"], 1)
        self.assertTrue(
            ready_target.scheduled_runs.filter(
                scheduled_for=timezone.make_aware(datetime(2026, 3, 22, 9, 0)),
                status=ScheduledPostRun.STATUS_SUCCESS,
            ).exists()
        )

    @override_settings(PUBLIC_APP_BASE_URL="https://example.com", SCHEDULER_PARTIAL_RETRY_MINUTES=15, META_RATE_LIMIT_BACKOFF_MINUTES=90)
    def test_markerless_backoff_does_not_create_long_retry_window(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-markerless", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:markerless-backoff",
            display_name="Markerless Backoff",
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        run = ScheduledPostRun.objects.create(target=target, scheduled_for=timezone.now() - timedelta(minutes=5))
        file_obj = {"id": "file-markerless", "name": "POST.mp4", "mimeType": "video/mp4"}
        now = timezone.now()

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]), patch(
            "scheduler.services.publishing.publish_platform",
            side_effect=PublishBackoff("Another publish run is already active for this target/slot."),
        ):
            outcome = process_scheduled_run(run, now=now)

        run.refresh_from_db()
        self.assertEqual(outcome, "backoff")
        self.assertEqual(run.status, ScheduledPostRun.STATUS_BACKOFF)
        self.assertEqual(run.next_retry_at, now + timedelta(minutes=15))

    @override_settings(PUBLIC_APP_BASE_URL="https://example.com", SCHEDULER_PARTIAL_RETRY_MINUTES=15)
    def test_partial_success_sets_retry_delay_and_retries_only_failed_platform_on_same_file(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-partial", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-partial", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:partial|ig:partial",
            display_name="Partial Pair",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            posting_times=["09:00"],
            default_caption="Caption",
        )
        slot = timezone.make_aware(datetime(2026, 3, 22, 9, 0))
        run = ScheduledPostRun.objects.create(target=target, scheduled_for=slot)
        file_obj = {"id": "file1", "name": "POST1.mp4", "mimeType": "video/mp4"}
        competing_file = {"id": "file2", "name": "POST2.mp4", "mimeType": "video/mp4"}
        now = timezone.make_aware(datetime(2026, 3, 22, 9, 30))

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]), patch(
            "scheduler.services.publishing._publish_to_facebook",
            return_value="fb-post",
        ) as facebook_mock, patch(
            "scheduler.services.publishing._publish_to_instagram",
            side_effect=PublishingError("temporary ig failure"),
        ):
            outcome = process_scheduled_run(run, now=now)

        run.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(run.status, ScheduledPostRun.STATUS_PARTIAL_SUCCESS)
        self.assertEqual(run.drive_file_id, "file1")
        self.assertEqual(run.platform_status[SocialAccount.FACEBOOK], PostLog.STATUS_SUCCESS)
        self.assertEqual(run.platform_status[SocialAccount.INSTAGRAM], PostLog.STATUS_FAILED)
        self.assertEqual(run.next_retry_at, now + timedelta(minutes=15))
        self.assertEqual(target.post_logs.filter(platform=SocialAccount.FACEBOOK, status=PostLog.STATUS_SUCCESS).count(), 1)
        facebook_mock.assert_called_once()

        with patch("scheduler.services.publishing.list_folder_files", return_value=[competing_file, file_obj]), patch(
            "scheduler.services.publishing._publish_to_facebook",
            return_value="duplicate-fb",
        ) as facebook_retry_mock, patch(
            "scheduler.services.publishing._publish_to_instagram",
            return_value="ig-post",
        ) as instagram_retry_mock:
            outcome = process_scheduled_run(run, now=now + timedelta(minutes=15))

        run.refresh_from_db()
        self.assertEqual(outcome, "success")
        self.assertEqual(run.status, ScheduledPostRun.STATUS_SUCCESS)
        self.assertIsNone(run.next_retry_at)
        facebook_retry_mock.assert_not_called()
        instagram_retry_mock.assert_called_once()
        self.assertEqual(instagram_retry_mock.call_args.args[1]["id"], "file1")
        self.assertEqual(target.post_logs.filter(platform=SocialAccount.FACEBOOK, status=PostLog.STATUS_SUCCESS).count(), 1)
        self.assertEqual(target.post_logs.filter(platform=SocialAccount.INSTAGRAM, status=PostLog.STATUS_SUCCESS).count(), 1)

    @override_settings(
        PUBLIC_APP_BASE_URL="https://example.com",
        SCHEDULER_PARTIAL_RETRY_MINUTES=0,
        SCHEDULER_PARTIAL_MAX_ATTEMPTS=3,
        SCHEDULER_MAX_RUNS_PER_TICK=5,
    )
    def test_partial_success_caps_attempts_then_allows_next_due_slot(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-partial-cap", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-partial-cap", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:partial-cap|ig:partial-cap",
            display_name="Partial Cap",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            posting_times=["09:00", "10:00"],
            default_caption="Caption",
        )
        slot1 = timezone.make_aware(datetime(2026, 3, 22, 9, 0))
        slot2 = timezone.make_aware(datetime(2026, 3, 22, 10, 0))
        file1 = {"id": "file1", "name": "POST1.mp4", "mimeType": "video/mp4"}
        file2 = {"id": "file2", "name": "POST2.mp4", "mimeType": "video/mp4"}
        run1 = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=slot1,
            drive_file_id=file1["id"],
            drive_file_name=file1["name"],
            drive_mime_type=file1["mimeType"],
            status=ScheduledPostRun.STATUS_PARTIAL_SUCCESS,
            platform_status={SocialAccount.FACEBOOK: PostLog.STATUS_SUCCESS, SocialAccount.INSTAGRAM: PostLog.STATUS_FAILED},
            attempt_count=2,
        )
        ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=slot2,
            drive_file_id=file2["id"],
            drive_file_name=file2["name"],
            drive_mime_type=file2["mimeType"],
        )
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=slot1,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id=file1["id"],
            drive_file_name=file1["name"],
        )

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file1, file2]), patch(
            "scheduler.services.publishing._publish_to_instagram",
            side_effect=PublishingError("permanent ig failure"),
        ):
            first_result = publish_due_targets(reference_time=timezone.make_aware(datetime(2026, 3, 22, 10, 30)))

        run1.refresh_from_db()
        self.assertEqual(first_result["failed"], 1)
        self.assertEqual(run1.status, ScheduledPostRun.STATUS_FAILED)
        self.assertIsNone(run1.next_retry_at)

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file1, file2]), patch(
            "scheduler.services.publishing._publish_to_facebook",
            return_value="fb-post-2",
        ), patch(
            "scheduler.services.publishing._publish_to_instagram",
            return_value="ig-post-2",
        ):
            second_result = publish_due_targets(reference_time=timezone.make_aware(datetime(2026, 3, 22, 10, 31)))

        run2 = ScheduledPostRun.objects.get(target=target, scheduled_for=slot2)
        self.assertEqual(second_result["success"], 1)
        self.assertEqual(run2.status, ScheduledPostRun.STATUS_SUCCESS)
        self.assertEqual(run2.drive_file_id, file2["id"])

    @override_settings(PUBLIC_APP_BASE_URL="https://example.com", SCHEDULER_PARTIAL_RETRY_MINUTES=15)
    def test_invalid_token_failure_does_not_partial_retry_after_platform_success(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-invalid-token", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-invalid-token", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:invalid-token|ig:invalid-token",
            display_name="Invalid Token Pair",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            posting_times=["09:00"],
            default_caption="Caption",
        )
        slot = timezone.make_aware(datetime(2026, 3, 22, 9, 0))
        run = ScheduledPostRun.objects.create(target=target, scheduled_for=slot)
        file_obj = {"id": "file1", "name": "POST1.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]), patch(
            "scheduler.services.publishing._publish_to_facebook",
            return_value="fb-post",
        ), patch(
            "scheduler.services.publishing._publish_to_instagram",
            side_effect=PublishingError(
                "Error validating access token: The session has been invalidated because the user changed their password"
            ),
        ):
            outcome = process_scheduled_run(run, now=timezone.make_aware(datetime(2026, 3, 22, 9, 30)))

        run.refresh_from_db()
        self.assertEqual(outcome, "failed")
        self.assertEqual(run.status, ScheduledPostRun.STATUS_FAILED)
        self.assertIsNone(run.next_retry_at)
        self.assertEqual(run.platform_status[SocialAccount.FACEBOOK], PostLog.STATUS_SUCCESS)
        self.assertEqual(run.platform_status[SocialAccount.INSTAGRAM], PostLog.STATUS_FAILED)


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

    @override_settings(PUBLIC_APP_BASE_URL="http://example.com")
    def test_public_base_requires_https(self):
        from .services.proxy import is_public_base_ready

        self.assertFalse(is_public_base_ready())


class AdminAuthGateTest(TestCase):
    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="admin", APP_ADMIN_PASSWORD="secret")
    def test_dashboard_requires_basic_auth_when_admin_credentials_are_configured(self):
        response = self.client.get(reverse("scheduler:dashboard"))
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["WWW-Authenticate"])

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="admin", APP_ADMIN_PASSWORD="secret")
    def test_dashboard_allows_valid_basic_auth(self):
        credentials = base64.b64encode(b"admin:secret").decode("ascii")
        response = self.client.get(reverse("scheduler:dashboard"), HTTP_AUTHORIZATION=f"Basic {credentials}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logout")
        self.assertContains(response, reverse("scheduler:logout"))
        self.assertContains(response, 'data-home-url="/"')
        self.assertContains(response, "credentials: \"omit\"")

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="admin", APP_ADMIN_PASSWORD="secret", APP_ADMIN_REALM="Test Realm")
    def test_logout_returns_basic_auth_challenge(self):
        response = self.client.get(reverse("scheduler:logout"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], 'Basic realm="Test Realm Logged Out"')
        self.assertContains(response, "Logged out", status_code=401)

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="admin", APP_ADMIN_PASSWORD="secret")
    def test_dashboard_disable_credential_preserves_accounts_and_targets(self):
        credentials = base64.b64encode(b"admin:secret").decode("ascii")
        credential = MetaCredential.objects.create(label="Token A", access_token="token-a")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-a", name="Page A")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-a",
            display_name="Page A",
            facebook_account=fb,
        )

        response = self.client.post(
            reverse("scheduler:dashboard"),
            {"action": "delete_credential", "credential_id": credential.id},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )

        self.assertEqual(response.status_code, 302)
        credential.refresh_from_db()
        target.refresh_from_db()
        self.assertFalse(credential.is_active)
        self.assertFalse(target.is_active)
        self.assertTrue(MetaCredential.objects.filter(pk=credential.pk).exists())
        self.assertTrue(SocialAccount.objects.filter(pk=fb.pk).exists())
        self.assertTrue(PublishingTarget.objects.filter(pk=target.pk).exists())
        event = MetaCredentialEvent.objects.get(action=MetaCredentialEvent.ACTION_ARCHIVED, credential=credential)
        self.assertEqual(event.snapshot["credential"]["id"], credential.id)
        self.assertEqual(event.snapshot["targets"][0]["id"], target.id)

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="admin", APP_ADMIN_PASSWORD="secret")
    def test_dashboard_restore_credential_reactivates_previously_active_targets(self):
        credentials = base64.b64encode(b"admin:secret").decode("ascii")
        credential = MetaCredential.objects.create(label="Token A", access_token="token-a")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-a", name="Page A")
        active_target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-a",
            display_name="Page A",
            facebook_account=fb,
            is_active=True,
        )
        inactive_target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:fb-b",
            display_name="Page B",
            is_active=False,
        )
        self.client.post(
            reverse("scheduler:dashboard"),
            {"action": "delete_credential", "credential_id": credential.id},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )

        response = self.client.post(
            reverse("scheduler:dashboard"),
            {"action": "restore_credential", "credential_id": credential.id},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )

        self.assertEqual(response.status_code, 302)
        credential.refresh_from_db()
        active_target.refresh_from_db()
        inactive_target.refresh_from_db()
        self.assertTrue(credential.is_active)
        self.assertTrue(active_target.is_active)
        self.assertFalse(inactive_target.is_active)
        self.assertTrue(MetaCredentialEvent.objects.filter(action=MetaCredentialEvent.ACTION_RESTORED, credential=credential).exists())

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="admin", APP_ADMIN_PASSWORD="secret")
    def test_dashboard_rejects_sync_for_disabled_credential(self):
        from unittest.mock import patch

        credentials = base64.b64encode(b"admin:secret").decode("ascii")
        credential = MetaCredential.objects.create(label="Disabled Token", access_token="token-a", is_active=False)

        with patch("scheduler.views.sync_credential_accounts") as sync_mock:
            response = self.client.post(
                reverse("scheduler:dashboard"),
                {"action": "sync_credential", "credential_id": credential.id},
                HTTP_AUTHORIZATION=f"Basic {credentials}",
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_not_called()
        self.assertContains(response, "Disabled Token is disabled")

    def test_admin_does_not_allow_hard_delete_for_core_posting_records(self):
        from .admin import MetaCredentialAdmin, PublishingTargetAdmin, SocialAccountAdmin
        from django.contrib.admin.sites import AdminSite

        site = AdminSite()
        self.assertFalse(MetaCredentialAdmin(MetaCredential, site).has_delete_permission(None))
        self.assertFalse(SocialAccountAdmin(SocialAccount, site).has_delete_permission(None))
        self.assertFalse(PublishingTargetAdmin(PublishingTarget, site).has_delete_permission(None))


@override_settings(SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="", APP_ADMIN_PASSWORD="")
class DashboardTargetListTest(TestCase):
    def test_dashboard_add_token_persists_credential_and_redirect_shows_it(self):
        from unittest.mock import patch

        with patch(
            "scheduler.views.sync_credential_accounts",
            return_value={"created": 0, "reused": 0, "skipped": 0, "missing": 0},
        ):
            response = self.client.post(
                reverse("scheduler:dashboard"),
                {"action": "add_token", "label": "Business Manager A", "access_token": "token-a"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(MetaCredential.objects.count(), 1)
        self.assertContains(response, "Business Manager A")
        self.assertEqual(MetaCredentialEvent.objects.filter(action=MetaCredentialEvent.ACTION_CREATED).count(), 1)

    def test_dashboard_add_token_sync_failure_keeps_credential_and_shows_error(self):
        from unittest.mock import patch
        from .services.meta import MetaAPIError

        with patch("scheduler.views.sync_credential_accounts", side_effect=MetaAPIError("Meta sync failed")):
            response = self.client.post(
                reverse("scheduler:dashboard"),
                {"action": "add_token", "label": "Business Manager B", "access_token": "token-b"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        credential = MetaCredential.objects.get()
        self.assertEqual(credential.label, "Business Manager B")
        self.assertContains(response, "Business Manager B")
        self.assertContains(response, "Token saved, but sync failed: Meta sync failed")
        self.assertTrue(MetaCredentialEvent.objects.filter(action=MetaCredentialEvent.ACTION_SYNC_FAILED, credential=credential).exists())

    def test_dashboard_sync_credential_failure_preserves_existing_rows_after_reload(self):
        from unittest.mock import patch
        from .services.meta import MetaAPIError

        credential = MetaCredential.objects.create(label="Existing Token", access_token="token-existing")

        with patch("scheduler.views.sync_credential_accounts", side_effect=MetaAPIError("Meta unavailable")):
            response = self.client.post(
                reverse("scheduler:dashboard"),
                {"action": "sync_credential", "credential_id": credential.id},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(MetaCredential.objects.filter(pk=credential.pk).exists())
        self.assertContains(response, "Existing Token")
        self.assertContains(response, "Sync failed: Meta unavailable")

    def test_dashboard_shows_targets_from_multiple_tokens_with_credential_labels(self):
        credential_a = MetaCredential.objects.create(label="Token A", access_token="token-a")
        credential_b = MetaCredential.objects.create(label="Token B", access_token="token-b")
        fb_a = credential_a.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-a", name="Page A")
        fb_b = credential_b.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-b", name="Page B")
        PublishingTarget.objects.create(
            credential=credential_a,
            sync_key="fb:fb-a",
            display_name="Page A",
            facebook_account=fb_a,
        )
        PublishingTarget.objects.create(
            credential=credential_b,
            sync_key="fb:fb-b",
            display_name="Page B",
            facebook_account=fb_b,
        )

        response = self.client.get(reverse("scheduler:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Page A")
        self.assertContains(response, "Page B")
        self.assertContains(response, "Token: Token A")
        self.assertContains(response, "Token: Token B")

    def test_dashboard_renders_queue_backoff_and_content_hints(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Token A", access_token="token-a")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-a", name="IG A")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:dashboard",
            display_name="Dashboard Queue",
            instagram_account=ig,
            drive_folder_id="folder",
        )
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="file-1",
            drive_file_name="POST1.mp4",
            message="Instagram publish failed: Authorization Error",
        )

        with patch("scheduler.services.health.list_folder_files", return_value=[{"id": "file-1", "name": "POST1.mp4", "mimeType": "video/mp4"}]):
            response = self.client.get(reverse("scheduler:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current: POST1.mp4")
        self.assertContains(response, "Pending: instagram")
        self.assertContains(response, "Backoff active")


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
    def test_clean_media_name_context_removes_automation_style_noise(self):
        cleaned = _clean_media_name_context("500+ Viral Health Awareness Reels by Digital Ceo Official57.mp4")
        self.assertEqual(cleaned, "Health Awareness by")

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

    def test_payload_quality_errors_flags_cleaned_filename_mirroring(self):
        errors = _payload_quality_errors(
            {
                "primary_caption": "Health Awareness by",
                "hashtags": ["#one", "#two"],
                "short_caption": "Short",
                "long_caption": "Long enough",
                "hindi_caption": "Hindi",
                "english_caption": "English",
                "hinglish_caption": "Hinglish",
                "translated_hindi": "Hindi translation",
                "translated_english": "English translation",
                "translated_hinglish": "Hinglish translation",
            },
            {"name": "500+ Viral Health Awareness Reels by Digital Ceo Official57.mp4"},
        )
        self.assertIn("primary_caption mirrors cleaned filename context too closely", errors)

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


@override_settings(SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="", APP_ADMIN_PASSWORD="")
class AIViewFlowTest(TestCase):
    @override_settings(AI_API_KEY="test-key", SECURE_SSL_REDIRECT=False, APP_ADMIN_USERNAME="", APP_ADMIN_PASSWORD="")
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


class TelegramReportTest(TestCase):
    def test_long_telegram_message_is_split_into_safe_chunks(self):
        message = ("TARGET 1\n" + ("Published at: 28 Mar 2026, 09:00 AM\n" * 300)).strip()

        chunks = _split_telegram_message(message)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= TELEGRAM_MESSAGE_MAX_LENGTH for chunk in chunks))
        self.assertEqual("".join(chunk + "\n" for chunk in chunks).replace("\n\n", "\n").strip(), message)


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

    def test_skipped_platform_allows_shared_queue_to_move_forward(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb-skip", name="FB")
        ig = credential.accounts.create(platform="instagram", external_id="ig-skip", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:skip|ig:skip",
            display_name="Skip Pair",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )

        from unittest.mock import patch
        files = [
            {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg"},
            {"id": "file2", "name": "POST2.jpeg", "mimeType": "image/jpeg"},
        ]
        slot = get_daily_slots(target)[0]
        target.post_logs.create(platform="facebook", scheduled_for=slot, status="success", drive_file_id="file1", drive_file_name="POST1.jpeg")
        target.post_logs.create(platform="instagram", scheduled_for=slot, status="skipped", drive_file_id="file1", drive_file_name="POST1.jpeg")

        with patch("scheduler.services.publishing.list_folder_files", return_value=files):
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

    def test_caption_txt_is_downloaded_through_drive_service(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:caption",
            display_name="Caption Target",
            drive_folder_id="folder",
        )
        files = [
            {"id": "caption-file", "name": "caption.txt", "mimeType": "text/plain"},
            {"id": "media-file", "name": "POST1.jpeg", "mimeType": "image/jpeg"},
        ]

        with patch("scheduler.services.publishing.list_folder_files", return_value=files), patch(
            "scheduler.services.publishing.download_drive_file",
            return_value="Caption via service account".encode("utf-8"),
        ) as download_mock:
            caption = build_caption(target)

        self.assertEqual(caption, "Caption via service account")
        download_mock.assert_called_once_with("caption-file")


class ComplianceTest(TestCase):
    @override_settings(PUBLIC_APP_BASE_URL="")
    def test_instagram_publish_is_blocked_without_public_base_url(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform="instagram", external_id="ig-1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:block",
            display_name="IG Block",
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )

        result = evaluate_publish_readiness(
            target,
            "instagram",
            {"id": "file1", "name": "POST1.mp4", "mimeType": "video/mp4"},
            "Caption",
        )

        self.assertTrue(result.is_blocked)
        self.assertIn("PUBLIC_APP_BASE_URL", " | ".join(result.blocking_issues))

    @override_settings(PUBLIC_APP_BASE_URL="https://demo.ngrok-free.dev")
    def test_temporary_public_host_is_reported_as_warning(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb-1", name="FB", access_token="page-token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:warn",
            display_name="FB Warn",
            facebook_account=fb,
            drive_folder_id="folder",
            default_caption="Caption",
        )

        result = evaluate_publish_readiness(
            target,
            "facebook",
            {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg"},
            "Caption",
        )

        self.assertFalse(result.is_blocked)
        self.assertIn("temporary tunnel host", " | ".join(result.warnings))

    def test_facebook_publish_requires_page_access_token(self):
        credential = MetaCredential.objects.create(label="Test", access_token="broad-token")
        fb = credential.accounts.create(platform="facebook", external_id="fb-no-token", name="FB", access_token="")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:no-page-token",
            display_name="FB No Page Token",
            facebook_account=fb,
            drive_folder_id="folder",
            default_caption="Caption",
        )

        result = evaluate_publish_readiness(
            target,
            "facebook",
            {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "1000"},
            "Caption",
        )

        self.assertTrue(result.is_blocked)
        self.assertIn("Page access token", " | ".join(result.blocking_issues))

    def test_facebook_photo_over_10mb_fails_preflight(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb-big", name="FB", access_token="page-token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:big-photo",
            display_name="FB Big Photo",
            facebook_account=fb,
            drive_folder_id="folder",
            default_caption="Caption",
        )

        result = evaluate_publish_readiness(
            target,
            "facebook",
            {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": str(10 * 1024 * 1024 + 1)},
            "Caption",
        )

        self.assertTrue(result.is_blocked)
        self.assertIn("10 MB", " | ".join(result.blocking_issues))


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

    @override_settings(PUBLIC_APP_BASE_URL="https://demo.ngrok-free.dev")
    def test_health_reports_temporary_public_host_warning(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(credential=credential, sync_key="fb:hostwarn", display_name="Host Warn")
        health = build_target_health(target)
        self.assertIn("temporary tunnel host", " | ".join(health["issues"]))

    def test_health_reports_instagram_video_and_filename_caption_readiness_warnings(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-health", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:health",
            display_name="IG Health",
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="POST1",
        )
        files = [{"id": "1", "name": "POST1.avi", "mimeType": "video/x-msvideo"}]

        with patch("scheduler.services.health.list_folder_files", return_value=files):
            health = build_target_health(target)

        issues = " | ".join(health["issues"])
        self.assertIn("unsupported video type", issues)
        self.assertIn("default caption matches a media filename", issues)

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90)
    def test_health_reports_current_file_pending_platform_and_backoff(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-health-backoff", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:health-backoff",
            display_name="IG Health Backoff",
            instagram_account=ig,
            drive_folder_id="folder",
        )
        file_obj = {"id": "file-1", "name": "POST1.mp4", "mimeType": "video/mp4"}
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="file-1",
            drive_file_name="POST1.mp4",
            message="Instagram publish failed: Authorization Error",
        )

        with patch("scheduler.services.health.list_folder_files", return_value=[file_obj]):
            health = build_target_health(target)

        self.assertEqual(health["current_file"]["name"], "POST1.mp4")
        self.assertEqual(health["pending_platforms"], ["instagram"])
        self.assertTrue(health["backoff_messages"])
        self.assertIn("Backoff active", " | ".join(health["issues"]))

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90, META_APP_RATE_LIMIT_BACKOFF_MINUTES=1440)
    def test_health_reports_credential_rate_limit_backoff(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        source_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-source-health", name="IG Source")
        source = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:source-health",
            display_name="Source Health",
            instagram_account=source_ig,
            drive_folder_id="source-folder",
        )
        rate_log = source.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="source-file",
            drive_file_name="SOURCE.mp4",
            message="Instagram publish failed: Application request limit reached | Meta error details: type=OAuthException, code=4",
        )
        PostLog.objects.filter(pk=rate_log.pk).update(created_at=timezone.now() - timedelta(hours=12))

        blocked_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-blocked-health", name="IG Blocked")
        blocked = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:blocked-health",
            display_name="Blocked Health",
            instagram_account=blocked_ig,
            drive_folder_id="blocked-folder",
        )
        file_obj = {"id": "pending-file", "name": "POST1.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.health.list_folder_files", return_value=[file_obj]):
            health = build_target_health(blocked)

        self.assertEqual(health["pending_platforms"], ["instagram"])
        self.assertIn("credential/platform", " | ".join(health["backoff_messages"]))

    def test_health_prefers_locked_scheduled_run_file_over_recomputed_current_file(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-health-lock", name="FB")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-health-lock", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="health:locked-run",
            display_name="Health Locked Run",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )
        slot = timezone.now()
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=slot,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id="file-1",
            drive_file_name="POST1.mp4",
        )
        run = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=slot,
            drive_file_id="file-2",
            drive_file_name="POST2.mp4",
            drive_mime_type="video/mp4",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.FACEBOOK: PostLog.STATUS_SUCCESS, SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() - timedelta(minutes=1),
            attempt_count=2,
        )
        files = [
            {"id": "file-1", "name": "POST1.mp4", "mimeType": "video/mp4"},
            {"id": "file-2", "name": "POST2.mp4", "mimeType": "video/mp4"},
        ]

        with patch("scheduler.services.health.list_folder_files", return_value=files):
            health = build_target_health(target)

        self.assertEqual(health["current_file"]["id"], "file-2")
        self.assertEqual(health["current_file_source"], "scheduled_run")
        self.assertEqual(health["pending_platforms"], [SocialAccount.INSTAGRAM])
        self.assertEqual(health["current_run"]["id"], run.id)
        self.assertEqual(health["current_run"]["next_retry_at"], run.next_retry_at)

    def test_health_ignores_deferred_run_when_reporting_current_file(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-health-deferred", name="FB")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-health-deferred", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="health:deferred-run",
            display_name="Health Deferred Run",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )
        slot = timezone.now()
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=slot,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id="file-1",
            drive_file_name="POST1.mp4",
        )
        ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=slot,
            drive_file_id="file-2",
            drive_file_name="POST2.mp4",
            drive_mime_type="video/mp4",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.FACEBOOK: PostLog.STATUS_SUCCESS, SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() + timedelta(hours=1),
        )
        files = [
            {"id": "file-1", "name": "POST1.mp4", "mimeType": "video/mp4"},
            {"id": "file-2", "name": "POST2.mp4", "mimeType": "video/mp4"},
        ]

        with patch("scheduler.services.health.list_folder_files", return_value=files):
            health = build_target_health(target)

        self.assertEqual(health["current_file"]["id"], "file-1")
        self.assertEqual(health["current_file_source"], "drive_scan")
        self.assertIsNone(health["current_run"])
        self.assertEqual(health["pending_platforms"], [SocialAccount.INSTAGRAM])

    def test_audit_publish_readiness_prints_operational_state(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Token Audit", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-audit", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:audit",
            display_name="Audit Target",
            instagram_account=ig,
            drive_folder_id="folder",
            posting_times=["09:00"],
        )
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="file-1",
            drive_file_name="POST1.mp4",
            message="Instagram publish failed: Authorization Error",
        )
        output = StringIO()

        with patch("scheduler.services.health.list_folder_files", return_value=[{"id": "file-1", "name": "POST1.mp4", "mimeType": "video/mp4"}]):
            call_command("audit_publish_readiness", stdout=output)

        text = output.getvalue()
        self.assertIn("token: Token Audit", text)
        self.assertIn("current_file: POST1.mp4", text)
        self.assertIn("pending_platforms: instagram", text)
        self.assertIn("backoff:", text)

    def test_diagnose_posting_today_prints_slot_and_blocker_summary(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Token Diagnose", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-diagnose", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:diagnose",
            display_name="Diagnose Target",
            instagram_account=ig,
            drive_folder_id="folder",
            posting_times=["09:00"],
        )
        file_obj = {"id": "file-1", "name": "POST1.mp4", "mimeType": "video/mp4"}
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="file-1",
            drive_file_name="POST1.mp4",
            message="Instagram publish failed: Authorization Error",
        )
        output = StringIO()

        with patch("scheduler.services.health.list_folder_files", return_value=[file_obj]), patch(
            "scheduler.services.publishing.list_folder_files",
            return_value=[file_obj],
        ):
            call_command("diagnose_posting_today", "--target-id", str(target.pk), stdout=output)

        text = output.getvalue()
        self.assertIn("Posting diagnosis", text)
        self.assertIn("Diagnose Target", text)
        self.assertIn("slots_today:", text)
        self.assertIn("today_activity:", text)


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

    def test_instagram_ready_image_fails_when_jpeg_cannot_fit_under_limit(self):
        from unittest.mock import patch

        image = Image.new("RGB", (800, 800), color=(25, 50, 75))
        source = BytesIO()
        image.save(source, format="PNG")

        with patch("scheduler.services.media_transform.INSTAGRAM_IMAGE_MAX_BYTES", 1):
            with self.assertRaisesMessage(ValueError, "under 8 MB"):
                build_instagram_ready_image(source.getvalue())


class MediaCacheTest(TestCase):
    def test_cached_asset_refreshes_when_drive_fingerprint_changes(self):
        from unittest.mock import patch
        from .services.cache import ensure_cached_asset

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="cache:fingerprint",
            display_name="Cache Fingerprint",
            drive_folder_id="folder",
        )
        file_obj = {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "5"}

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(MEDIA_CACHE_DIR=temp_dir):
            with patch(
                "scheduler.services.cache.get_drive_file_metadata",
                side_effect=[
                    {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "5", "modifiedTime": "one"},
                    {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "6", "modifiedTime": "two"},
                ],
            ), patch("scheduler.services.cache.download_drive_file", side_effect=[b"first", b"second"]) as download_mock:
                first = ensure_cached_asset(target, file_obj)
                second = ensure_cached_asset(target, file_obj)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(download_mock.call_count, 2)
        second.refresh_from_db()
        self.assertEqual(second.source_fingerprint.split("|")[0], "two")
        self.assertEqual(second.file_size, 6)

    def test_blank_existing_fingerprint_does_not_skip_refresh(self):
        from unittest.mock import patch
        from .services.cache import ensure_cached_asset

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="cache:blank-fingerprint",
            display_name="Cache Blank Fingerprint",
            drive_folder_id="folder",
        )
        file_obj = {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "5"}

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(MEDIA_CACHE_DIR=temp_dir):
            path = f"{temp_dir}/cached-file"
            with open(path, "wb") as handle:
                handle.write(b"stale")
            MediaAsset.objects.create(
                target=target,
                drive_file_id="file1",
                drive_file_name="POST1.jpeg",
                variant="default",
                public_filename="POST1.jpeg",
                local_path=path,
                source_mime_type="image/jpeg",
                source_fingerprint="",
                content_type="image/jpeg",
                file_size=5,
                status=MediaAsset.STATUS_READY,
            )

            with patch(
                "scheduler.services.cache.get_drive_file_metadata",
                return_value={
                    "id": "file1",
                    "name": "POST1.jpeg",
                    "mimeType": "image/jpeg",
                    "size": "5",
                    "modifiedTime": "fresh",
                    "md5Checksum": "checksum",
                },
            ), patch("scheduler.services.cache.download_drive_file", return_value=b"fresh") as download_mock:
                asset = ensure_cached_asset(target, file_obj)

            download_mock.assert_called_once()
            asset.refresh_from_db()
            self.assertEqual(asset.source_fingerprint.split("|")[0], "fresh")
            self.assertNotEqual(asset.source_fingerprint, "")
            self.assertEqual(Path(asset.local_path).read_bytes(), b"fresh")
            self.assertEqual(asset.file_size, len(b"fresh"))

    def test_cache_failure_is_persisted(self):
        from unittest.mock import patch
        from .services.cache import ensure_cached_asset

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="cache:failure",
            display_name="Cache Failure",
            drive_folder_id="folder",
        )
        file_obj = {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "5"}

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(MEDIA_CACHE_DIR=temp_dir), patch(
            "scheduler.services.cache.get_drive_file_metadata",
            return_value={"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "5"},
        ), patch("scheduler.services.cache.download_drive_file", side_effect=RuntimeError("drive exploded")):
            with self.assertRaisesMessage(RuntimeError, "drive exploded"):
                ensure_cached_asset(target, file_obj)

        asset = MediaAsset.objects.get(target=target, drive_file_id="file1")
        self.assertEqual(asset.status, MediaAsset.STATUS_FAILED)
        self.assertIn("drive exploded", asset.last_error)
        self.assertIsNotNone(asset.last_synced_at)

    def test_ready_cache_refresh_failure_preserves_existing_local_file(self):
        from unittest.mock import patch
        from .services.cache import ensure_cached_asset

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="cache:ready-refresh-failure",
            display_name="Cache Ready Refresh Failure",
            drive_folder_id="folder",
        )
        file_obj = {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "5"}

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(MEDIA_CACHE_DIR=temp_dir):
            path = Path(temp_dir) / "cached-file"
            path.write_bytes(b"stale")
            MediaAsset.objects.create(
                target=target,
                drive_file_id="file1",
                drive_file_name="POST1.jpeg",
                variant="default",
                public_filename="POST1.jpeg",
                local_path=str(path),
                source_mime_type="image/jpeg",
                source_fingerprint="old",
                content_type="image/jpeg",
                file_size=len(b"stale"),
                status=MediaAsset.STATUS_READY,
            )

            with patch(
                "scheduler.services.cache.get_drive_file_metadata",
                return_value={"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg", "size": "6", "modifiedTime": "new"},
            ), patch("scheduler.services.cache.download_drive_file", side_effect=RuntimeError("drive exploded")):
                with self.assertRaisesMessage(RuntimeError, "drive exploded"):
                    ensure_cached_asset(target, file_obj)

            asset = MediaAsset.objects.get(target=target, drive_file_id="file1")
            self.assertEqual(asset.status, MediaAsset.STATUS_READY)
            self.assertIn("drive exploded", asset.last_error)
            self.assertEqual(Path(asset.local_path).read_bytes(), b"stale")

    def test_video_cache_cleans_temp_file_after_stream_failure(self):
        from unittest.mock import patch
        from .services.cache import ensure_cached_asset

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="cache:video-stream-failure",
            display_name="Cache Video Stream Failure",
            drive_folder_id="folder",
        )
        file_obj = {"id": "file-video", "name": "POST1.mp4", "mimeType": "video/mp4", "size": "12"}

        def fail_stream(_file_id, destination_path):
            Path(destination_path).write_bytes(b"partial")
            raise RuntimeError("stream failed")

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(MEDIA_CACHE_DIR=temp_dir), patch(
            "scheduler.services.cache.get_drive_file_metadata",
            return_value={"id": "file-video", "name": "POST1.mp4", "mimeType": "video/mp4", "size": "12"},
        ), patch("scheduler.services.cache.download_drive_file_to_path", side_effect=fail_stream):
            with self.assertRaisesMessage(RuntimeError, "stream failed"):
                ensure_cached_asset(target, file_obj)
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

        asset = MediaAsset.objects.get(target=target, drive_file_id="file-video")
        self.assertEqual(asset.status, MediaAsset.STATUS_FAILED)

    def test_video_cache_uses_streaming_download(self):
        from unittest.mock import patch
        from .services.cache import ensure_cached_asset

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="cache:video-stream",
            display_name="Cache Video Stream",
            drive_folder_id="folder",
        )
        file_obj = {"id": "file-video", "name": "POST1.mp4", "mimeType": "video/mp4", "size": "12"}

        def fake_stream(_file_id, destination_path):
            Path(destination_path).write_bytes(b"video-bytes")
            return len(b"video-bytes")

        with tempfile.TemporaryDirectory() as temp_dir, override_settings(MEDIA_CACHE_DIR=temp_dir), patch(
            "scheduler.services.cache.get_drive_file_metadata",
            return_value={"id": "file-video", "name": "POST1.mp4", "mimeType": "video/mp4", "size": "12"},
        ), patch("scheduler.services.cache.download_drive_file") as bytes_mock, patch(
            "scheduler.services.cache.download_drive_file_to_path",
            side_effect=fake_stream,
        ) as stream_mock:
            asset = ensure_cached_asset(target, file_obj)
            cached_bytes = Path(asset.local_path).read_bytes()

        bytes_mock.assert_not_called()
        stream_mock.assert_called_once()
        self.assertEqual(asset.file_size, len(b"video-bytes"))
        self.assertEqual(cached_bytes, b"video-bytes")


class InstagramPublishTest(TestCase):
    def test_publishing_graph_get_reports_meta_error_details(self):
        from unittest.mock import MagicMock, patch
        from .services.publishing import _graph_get

        response = MagicMock()
        response.status_code = 400
        response.text = '{"error": "..."}'
        response.json.return_value = {
            "error": {
                "message": "Application request limit reached",
                "type": "OAuthException",
                "code": 4,
                "error_subcode": 2207008,
                "fbtrace_id": "TRACE123",
            }
        }

        with patch("scheduler.services.publishing._request_with_retries", return_value=response):
            with self.assertRaises(PublishingError) as ctx:
                _graph_get("/container", "token")

        message = str(ctx.exception)
        self.assertIn("Application request limit reached", message)
        self.assertIn("type=OAuthException", message)
        self.assertIn("code=4", message)
        self.assertIn("subcode=2207008", message)
        self.assertIn("trace=TRACE123", message)

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

        with patch("scheduler.services.publishing._check_instagram_content_publishing_limit"), patch(
            "scheduler.services.publishing.get_cached_public_urls", return_value=["https://example.com/POST15.jpg"]
        ), patch(
            "scheduler.services.publishing._graph_post",
            side_effect=[{"id": "container-1"}, {"id": "publish-1"}],
        ) as graph_post_mock, patch("scheduler.services.publishing._wait_for_instagram_container") as wait_mock:
            result = _publish_to_instagram(target, file_obj)

        self.assertEqual(result, "publish-1")
        wait_mock.assert_called_once_with("container-1", "page-token")
        self.assertEqual(graph_post_mock.call_count, 2)

    def test_instagram_publish_falls_back_when_container_status_poll_is_unauthorized(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:fallback",
            display_name="IG Fallback",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file1", "name": "POST16.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing._check_instagram_content_publishing_limit"), patch(
            "scheduler.services.publishing.get_cached_public_urls", return_value=["https://example.com/POST16.mp4"]
        ), patch(
            "scheduler.services.publishing._graph_post",
            side_effect=[{"id": "container-1"}, {"id": "publish-1"}],
        ) as graph_post_mock, patch(
            "scheduler.services.publishing._wait_for_instagram_container",
            side_effect=PublishingError("Authorization Error"),
        ) as wait_mock, patch("scheduler.services.publishing.time.sleep") as sleep_mock:
            result = _publish_to_instagram(target, file_obj)

        self.assertEqual(result, "publish-1")
        wait_mock.assert_called_once_with("container-1", "page-token")
        sleep_mock.assert_not_called()
        self.assertEqual(graph_post_mock.call_count, 2)
        self.assertEqual(graph_post_mock.call_args.args[0], "/ig1/media_publish")

    def test_instagram_publish_retries_direct_publish_when_fallback_container_is_not_ready(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:fallback-retry",
            display_name="IG Fallback Retry",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file1", "name": "POST17.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing._check_instagram_content_publishing_limit"), patch(
            "scheduler.services.publishing.get_cached_public_urls", return_value=["https://example.com/POST17.mp4"]
        ), patch(
            "scheduler.services.publishing._graph_post",
            side_effect=[
                {"id": "container-1"},
                PublishingError("Media ID is not available"),
                {"id": "publish-1"},
            ],
        ) as graph_post_mock, patch(
            "scheduler.services.publishing._wait_for_instagram_container",
            side_effect=PublishingError("Authorization Error"),
        ), patch("scheduler.services.publishing.time.sleep") as sleep_mock:
            result = _publish_to_instagram(target, file_obj)

        self.assertEqual(result, "publish-1")
        self.assertEqual(graph_post_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_instagram_publish_reuses_recent_uncertain_container_before_creating_new_one(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:reuse-container",
            display_name="IG Reuse Container",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file1", "name": "POST17.mp4", "mimeType": "video/mp4"}
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id=file_obj["id"],
            drive_file_name=file_obj["name"],
            meta_creation_id="container-old",
            message="Instagram status polling failed after container creation.",
        )

        with patch("scheduler.services.publishing._check_instagram_content_publishing_limit"), patch(
            "scheduler.services.publishing.get_cached_public_urls", return_value=["https://example.com/POST17.mp4"]
        ), patch("scheduler.services.publishing._graph_post", return_value={"id": "publish-1"}) as graph_post_mock:
            result = _publish_to_instagram(target, file_obj)

        self.assertEqual(result, "publish-1")
        graph_post_mock.assert_called_once()
        self.assertEqual(graph_post_mock.call_args.args[0], "/ig1/media_publish")

    @override_settings(PUBLIC_APP_BASE_URL="https://example.com")
    def test_publish_platform_records_uncertain_instagram_container_id_on_failure(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig-record", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:record-container",
            display_name="IG Record Container",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file-record", "name": "POST18.jpeg", "mimeType": "image/jpeg"}

        with patch("scheduler.services.publishing._check_instagram_content_publishing_limit"), patch(
            "scheduler.services.publishing.get_cached_public_urls", return_value=["https://example.com/POST18.jpg"]
        ), patch("scheduler.services.publishing._wait_for_instagram_container"), patch(
            "scheduler.services.publishing._graph_post",
            side_effect=[{"id": "container-new"}, PublishingError("Media ID is not available")],
        ):
            with self.assertRaises(PublishingError):
                publish_platform(target, SocialAccount.INSTAGRAM, file_obj=file_obj)

        log = target.post_logs.get(platform=SocialAccount.INSTAGRAM)
        self.assertEqual(log.meta_creation_id, "container-new")
        self.assertIn("publish state is uncertain", log.message)

    def test_instagram_content_publishing_limit_blocks_before_container_creation(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig-limit", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:limit",
            display_name="IG Limit",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )

        with patch(
            "scheduler.services.publishing._graph_get",
            return_value={"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]},
        ), patch("scheduler.services.publishing._graph_post") as graph_post_mock:
            with self.assertRaisesMessage(PublishingError, "content publishing limit"):
                _publish_to_instagram(target, {"id": "file1", "name": "POST1.jpeg", "mimeType": "image/jpeg"})

        graph_post_mock.assert_not_called()

    def test_instagram_container_wait_handles_terminal_statuses(self):
        from unittest.mock import patch

        with patch("scheduler.services.publishing._graph_get", return_value={"status_code": "FINISHED"}), patch(
            "scheduler.services.publishing.time.sleep"
        ) as sleep_mock:
            _wait_for_instagram_container("container", "token")

        sleep_mock.assert_not_called()

        with patch("scheduler.services.publishing._graph_get", return_value={"status_code": "ERROR"}):
            with self.assertRaisesMessage(PublishingError, "status ERROR"):
                _wait_for_instagram_container("container", "token")

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90)
    def test_recent_meta_rate_limit_failure_skips_instagram_retry_without_meta_call(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform="instagram", external_id="ig1", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-limit",
            display_name="IG Rate Limit",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file-rate-limit", "name": "POST18.mp4", "mimeType": "video/mp4"}
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id=file_obj["id"],
            drive_file_name=file_obj["name"],
            message="Instagram publish failed: Application request limit reached",
        )

        with patch("scheduler.services.publishing._publish_to_instagram") as publish_mock:
            with self.assertRaisesMessage(PublishingError, "Meta rate limit backoff active"):
                publish_platform(target, SocialAccount.INSTAGRAM, file_obj=file_obj)

        publish_mock.assert_not_called()
        self.assertEqual(target.post_logs.filter(drive_file_id=file_obj["id"]).count(), 1)

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90, META_APP_RATE_LIMIT_BACKOFF_MINUTES=1440)
    def test_instagram_publish_action_limit_does_not_block_other_credential_targets(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        first_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-rate-source", name="IG Source")
        first_target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-source",
            display_name="Rate Source",
            instagram_account=first_ig,
            drive_folder_id="folder-source",
        )
        second_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-rate-blocked", name="IG Blocked")
        second_target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-blocked",
            display_name="Rate Blocked",
            instagram_account=second_ig,
            drive_folder_id="folder-blocked",
            default_caption="Caption",
        )
        rate_log = first_target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="source-file",
            drive_file_name="SOURCE.mp4",
            message="Instagram publish failed: Application request limit reached | Meta error details: type=OAuthException, code=4, subcode=2207051",
        )
        PostLog.objects.filter(pk=rate_log.pk).update(created_at=timezone.now() - timedelta(hours=12))
        file_obj = {"id": "new-file", "name": "POST19.mp4", "mimeType": "video/mp4"}

        with override_settings(PUBLIC_APP_BASE_URL="https://example.com"), patch(
            "scheduler.services.publishing._publish_to_instagram",
            return_value="ig-published",
        ) as publish_mock:
            publish_platform(second_target, SocialAccount.INSTAGRAM, file_obj=file_obj)

        publish_mock.assert_called_once()
        self.assertTrue(second_target.post_logs.filter(drive_file_id=file_obj["id"], status=PostLog.STATUS_SUCCESS).exists())

    def test_platform_publish_lock_prevents_parallel_duplicate_publish(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-lock", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:lock",
            display_name="Lock Target",
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        file_obj = {"id": "file-lock", "name": "POST.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing.cache.add", return_value=False), patch(
            "scheduler.services.publishing._publish_to_instagram"
        ) as publish_mock:
            with self.assertRaisesMessage(PublishBackoff, "already active"):
                publish_platform(target, SocialAccount.INSTAGRAM, file_obj=file_obj)

        publish_mock.assert_not_called()
        self.assertFalse(target.post_logs.filter(drive_file_id=file_obj["id"]).exists())

    def test_release_scoped_instagram_backoff_clears_other_target_credential_block(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-release", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:release",
            display_name="Release Backoff",
            instagram_account=ig,
            drive_folder_id="folder",
            last_status="backoff",
            last_error="instagram: Meta rate limit backoff active for this credential/platform.",
        )
        run = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=1),
            drive_file_id="file-release",
            drive_file_name="POST.mp4",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() + timedelta(hours=12),
            last_error="instagram: Meta rate limit backoff active for this credential/platform.",
        )

        output = StringIO()
        call_command("release_scoped_instagram_backoff", "--credential-label", "Test", "--apply", stdout=output)

        run.refresh_from_db()
        target.refresh_from_db()
        self.assertIn("released=1", output.getvalue())
        self.assertEqual(run.status, ScheduledPostRun.STATUS_PENDING)
        self.assertEqual(run.platform_status[SocialAccount.INSTAGRAM], ScheduledPostRun.STATUS_PENDING)
        self.assertIsNone(run.next_retry_at)
        self.assertEqual(target.last_error, "")

    def test_release_scoped_instagram_backoff_releases_one_run_per_target_file(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-release-one", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:release-one",
            display_name="Release One",
            instagram_account=ig,
            drive_folder_id="folder",
        )
        first = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=2),
            drive_file_id="file-release",
            drive_file_name="POST.mp4",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() + timedelta(hours=12),
            last_error="instagram: Meta rate limit backoff active for this credential/platform.",
        )
        second = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=1),
            drive_file_id="file-release",
            drive_file_name="POST.mp4",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() + timedelta(hours=12),
            last_error="instagram: Meta rate limit backoff active for this credential/platform.",
        )

        output = StringIO()
        call_command("release_scoped_instagram_backoff", "--credential-label", "Test", "--apply", stdout=output)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIn("released=1", output.getvalue())
        statuses = sorted([first.status, second.status])
        self.assertEqual(statuses, sorted([ScheduledPostRun.STATUS_PENDING, ScheduledPostRun.STATUS_BACKOFF]))

    def test_release_scoped_instagram_backoff_keeps_target_file_rate_limit(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-keep", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:keep",
            display_name="Keep Backoff",
            instagram_account=ig,
            drive_folder_id="folder",
        )
        run = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=1),
            drive_file_id="file-keep",
            drive_file_name="POST.mp4",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() + timedelta(hours=12),
            last_error="instagram: Meta rate limit backoff active for this credential/platform.",
        )
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=run.scheduled_for,
            status=PostLog.STATUS_FAILED,
            drive_file_id=run.drive_file_id,
            drive_file_name=run.drive_file_name,
            message="Application request limit reached | Meta error details: type=OAuthException, code=4, subcode=2207051",
        )

        output = StringIO()
        call_command("release_scoped_instagram_backoff", "--credential-label", "Test", "--apply", stdout=output)

        run.refresh_from_db()
        self.assertIn("released=0", output.getvalue())
        self.assertEqual(run.status, ScheduledPostRun.STATUS_BACKOFF)
        self.assertIsNotNone(run.next_retry_at)

    def test_reconcile_successful_scheduled_runs_marks_pending_file_complete(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-reconcile", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-reconcile", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:reconcile|ig:reconcile",
            display_name="Reconcile",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )
        run = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=1),
            drive_file_id="file-reconcile",
            drive_file_name="POST.mp4",
            status=ScheduledPostRun.STATUS_PENDING,
            platform_status={SocialAccount.FACEBOOK: PostLog.STATUS_SUCCESS, SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_PENDING},
        )
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=run.scheduled_for,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id=run.drive_file_id,
            drive_file_name=run.drive_file_name,
        )
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=run.scheduled_for,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id=run.drive_file_id,
            drive_file_name=run.drive_file_name,
        )

        output = StringIO()
        call_command("reconcile_successful_scheduled_runs", "--target-id", str(target.id), "--apply", stdout=output)

        run.refresh_from_db()
        self.assertIn("reconciled=1", output.getvalue())
        self.assertEqual(run.status, ScheduledPostRun.STATUS_SUCCESS)
        self.assertEqual(run.platform_status[SocialAccount.INSTAGRAM], PostLog.STATUS_SUCCESS)

    def test_skip_media_platform_marks_run_terminal_without_success_log(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-skip-command", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-skip-command", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:skip-command|ig:skip-command",
            display_name="Skip Command",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
        )
        run = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=1),
            drive_file_id="file-skip",
            drive_file_name="POST.png",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.FACEBOOK: PostLog.STATUS_SUCCESS, SocialAccount.INSTAGRAM: ScheduledPostRun.STATUS_BACKOFF},
            next_retry_at=timezone.now() + timedelta(hours=12),
            last_error="instagram blocked",
        )
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=run.scheduled_for,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id=run.drive_file_id,
            drive_file_name=run.drive_file_name,
        )

        output = StringIO()
        call_command(
            "skip_media_platform",
            "--target-id",
            str(target.id),
            "--drive-file-id",
            "file-skip",
            "--platform",
            SocialAccount.INSTAGRAM,
            "--apply",
            stdout=output,
        )

        run.refresh_from_db()
        self.assertIn("UPDATED", output.getvalue())
        self.assertEqual(run.status, ScheduledPostRun.STATUS_SKIPPED)
        self.assertEqual(run.platform_status[SocialAccount.INSTAGRAM], PostLog.STATUS_SKIPPED)
        self.assertTrue(target.post_logs.filter(platform=SocialAccount.INSTAGRAM, drive_file_id="file-skip", status=PostLog.STATUS_SKIPPED).exists())

    def test_reconcile_live_post_marks_verified_platform_success(self):
        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform=SocialAccount.FACEBOOK, external_id="fb-live", name="FB", access_token="page-token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-live", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:live|ig:live",
            display_name="Live Reconcile",
            facebook_account=fb,
            instagram_account=ig,
            drive_folder_id="folder",
            last_status="backoff",
            last_error="instagram blocked",
        )
        run = ScheduledPostRun.objects.create(
            target=target,
            scheduled_for=timezone.now() - timedelta(hours=1),
            drive_file_id="file-live",
            drive_file_name="POST.png",
            status=ScheduledPostRun.STATUS_BACKOFF,
            platform_status={SocialAccount.FACEBOOK: PostLog.STATUS_SUCCESS, SocialAccount.INSTAGRAM: PostLog.STATUS_FAILED},
            next_retry_at=timezone.now() + timedelta(hours=12),
            last_error="instagram: Application request limit reached",
        )
        target.post_logs.create(
            platform=SocialAccount.FACEBOOK,
            scheduled_for=run.scheduled_for,
            status=PostLog.STATUS_SUCCESS,
            drive_file_id=run.drive_file_id,
            drive_file_name=run.drive_file_name,
        )

        output = StringIO()
        call_command(
            "reconcile_live_post",
            "--target-id",
            str(target.id),
            "--drive-file-id",
            "file-live",
            "--platform",
            SocialAccount.INSTAGRAM,
            "--apply",
            stdout=output,
        )

        run.refresh_from_db()
        target.refresh_from_db()
        self.assertIn("UPDATED", output.getvalue())
        self.assertEqual(run.status, ScheduledPostRun.STATUS_SUCCESS)
        self.assertEqual(run.platform_status[SocialAccount.INSTAGRAM], PostLog.STATUS_SUCCESS)
        self.assertIsNone(run.next_retry_at)
        self.assertEqual(target.last_status, "success")
        self.assertTrue(target.post_logs.filter(platform=SocialAccount.INSTAGRAM, drive_file_id="file-live", status=PostLog.STATUS_SUCCESS).exists())

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90, META_APP_RATE_LIMIT_BACKOFF_MINUTES=1440)
    def test_credential_backoff_retry_uses_original_failure_expiry(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        source_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-rate-source", name="IG Source")
        source = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-source-expiry",
            display_name="Rate Source Expiry",
            instagram_account=source_ig,
            drive_folder_id="folder-source",
        )
        rate_log = source.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="source-file",
            drive_file_name="SOURCE.mp4",
            message="Instagram publish failed: Application request limit reached | Meta error details: type=OAuthException, code=4",
        )
        failure_time = timezone.now() - timedelta(hours=12)
        PostLog.objects.filter(pk=rate_log.pk).update(created_at=failure_time)

        blocked_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-rate-blocked-expiry", name="IG Blocked")
        blocked = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-blocked-expiry",
            display_name="Rate Blocked Expiry",
            instagram_account=blocked_ig,
            drive_folder_id="folder-blocked",
            default_caption="Caption",
        )
        run = ScheduledPostRun.objects.create(target=blocked, scheduled_for=timezone.now() - timedelta(minutes=10))
        file_obj = {"id": "new-file", "name": "POST19.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing.list_folder_files", return_value=[file_obj]):
            outcome = process_scheduled_run(run)

        run.refresh_from_db()
        self.assertEqual(outcome, "backoff")
        self.assertEqual(run.status, ScheduledPostRun.STATUS_BACKOFF)
        expected_retry_at = failure_time + timedelta(minutes=1440)
        self.assertLess(abs((run.next_retry_at - expected_retry_at).total_seconds()), 2)
        self.assertLess((run.next_retry_at - timezone.now()).total_seconds(), 13 * 60 * 60)

    @override_settings(META_RATE_LIMIT_BACKOFF_MINUTES=90, META_APP_RATE_LIMIT_BACKOFF_MINUTES=1440)
    def test_real_success_clears_older_credential_rate_limit_backoff(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        source_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-rate-source-clear", name="IG Source")
        source = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-source-clear",
            display_name="Rate Source Clear",
            instagram_account=source_ig,
            drive_folder_id="folder-source",
        )
        rate_log = source.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="source-file",
            drive_file_name="SOURCE.mp4",
            message="Instagram publish failed: Application request limit reached | Meta error details: type=OAuthException, code=4",
        )
        PostLog.objects.filter(pk=rate_log.pk).update(created_at=timezone.now() - timedelta(hours=12))
        source.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_SUCCESS,
            drive_file_id="cleared-file",
            drive_file_name="CLEARED.mp4",
            meta_creation_id="17890000000000000",
        )

        blocked_ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-rate-clear-target", name="IG Clear Target")
        blocked = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:rate-clear-target",
            display_name="Rate Clear Target",
            instagram_account=blocked_ig,
            drive_folder_id="folder-blocked",
            default_caption="Caption",
        )
        file_obj = {"id": "new-file", "name": "POST20.mp4", "mimeType": "video/mp4"}

        with override_settings(PUBLIC_APP_BASE_URL="https://example.com"), patch(
            "scheduler.services.publishing._publish_to_instagram",
            return_value="ig-published",
        ) as publish_mock:
            publish_platform(blocked, SocialAccount.INSTAGRAM, file_obj=file_obj)

        publish_mock.assert_called_once()
        self.assertTrue(blocked.post_logs.filter(drive_file_id=file_obj["id"], status=PostLog.STATUS_SUCCESS).exists())

    @override_settings(PUBLIC_APP_BASE_URL="https://example.com", META_APP_RATE_LIMIT_BACKOFF_MINUTES=1440)
    def test_synthetic_backoff_message_does_not_create_fresh_backoff_window(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        ig = credential.accounts.create(platform=SocialAccount.INSTAGRAM, external_id="ig-synthetic", name="IG")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="ig:synthetic-backoff",
            display_name="Synthetic Backoff",
            instagram_account=ig,
            drive_folder_id="folder",
            default_caption="Caption",
        )
        target.post_logs.create(
            platform=SocialAccount.INSTAGRAM,
            scheduled_for=timezone.now(),
            status=PostLog.STATUS_FAILED,
            drive_file_id="file-synthetic",
            drive_file_name="POST21.mp4",
            message="Meta rate limit backoff active for this credential/platform. Skipping publish retry for up to 1440 minutes after the latest rate-limit response.",
        )
        file_obj = {"id": "file-synthetic", "name": "POST21.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing._publish_to_instagram", return_value="ig-published") as publish_mock:
            publish_platform(target, SocialAccount.INSTAGRAM, file_obj=file_obj)

        publish_mock.assert_called_once()
        self.assertTrue(target.post_logs.filter(drive_file_id=file_obj["id"], status=PostLog.STATUS_SUCCESS).exists())


class FacebookPublishTest(TestCase):
    def test_facebook_video_publish_does_not_send_filename_based_title(self):
        from unittest.mock import patch

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb1", name="FB", access_token="page-token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:video",
            display_name="FB Video",
            facebook_account=fb,
            drive_folder_id="folder",
            default_caption="Human caption",
        )
        file_obj = {"id": "file1", "name": "500+ Viral Health Awareness Reels by Digital Ceo Official57.mp4", "mimeType": "video/mp4"}

        with patch("scheduler.services.publishing.ensure_cached_asset") as ensure_asset_mock, patch(
            "scheduler.services.publishing._graph_post_multipart",
            return_value={"id": "video-1"},
        ) as post_multipart_mock:
            asset = ensure_asset_mock.return_value
            asset.local_path = __file__
            asset.public_filename = "video.mp4"
            asset.content_type = "video/mp4"
            result = _publish_to_facebook(target, file_obj)

        self.assertEqual(result, "video-1")
        payload = post_multipart_mock.call_args.args[2]
        self.assertNotIn("title", payload)
        self.assertEqual(payload["description"], "Human caption")


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


class RunDuePostsCommandTest(TestCase):
    def test_run_due_posts_reports_skip_backoff_counts(self):
        from unittest.mock import patch

        output = StringIO()
        with patch(
            "scheduler.management.commands.run_due_posts.publish_due_targets",
            return_value={
                "checked_at": timezone.now(),
                "checked_targets": 3,
                "success": 1,
                "failed": 0,
                "skipped": 2,
                "backoff": 1,
                "content_exhausted": 1,
            },
        ):
            call_command("run_due_posts", stdout=output)

        text = output.getvalue()
        self.assertIn("Checked=3", text)
        self.assertIn("Skipped=2", text)
        self.assertIn("Backoff=1", text)
        self.assertIn("ContentExhausted=1", text)


class MetricsExportTest(TestCase):
    def test_fetch_facebook_metrics_resolves_reel_video_id_via_page_posts(self):
        from unittest.mock import patch

        def fake_graph_get(path, access_token, params=None):
            if path == "/page-1/posts":
                return {
                    "data": [
                        {
                            "id": "page-1_post-1",
                            "permalink_url": "https://www.facebook.com/reel/1492376226009478/",
                        }
                    ]
                }
            if path == "/page-1_post-1":
                return {
                    "id": "page-1_post-1",
                    "permalink_url": "https://www.facebook.com/reel/1492376226009478/",
                    "created_time": "2026-04-10T04:34:56+0000",
                    "comments": {"summary": {"total_count": 3}},
                    "reactions": {"summary": {"total_count": 7}},
                    "shares": {"count": 2},
                }
            if path == "/1492376226009478":
                return {
                    "id": "1492376226009478",
                    "permalink_url": "https://www.facebook.com/reel/1492376226009478/",
                    "created_time": "2026-04-10T04:34:56+0000",
                    "likes": {"summary": {"total_count": 7}},
                    "comments": {"summary": {"total_count": 3}},
                }
            if path == "/page-1_post-1/insights":
                metric = params["metric"]
                values = {
                    "post_impressions": 100,
                    "post_impressions_unique": 80,
                    "post_engaged_users": 12,
                }
                return {"data": [{"values": [{"value": values[metric]}]}]}
            if path == "/1492376226009478/insights":
                return {"data": [{"values": [{"value": 45}]}]}
            raise AssertionError(f"Unexpected path: {path}")

        with patch("scheduler.services.metrics._graph_get", side_effect=fake_graph_get):
            metrics = fetch_facebook_metrics("1492376226009478", "token", "page-1")

        self.assertEqual(metrics["id"], "page-1_post-1")
        self.assertEqual(metrics["permalink_url"], "https://www.facebook.com/reel/1492376226009478/")
        self.assertEqual(metrics["reaction_count"], "7")
        self.assertEqual(metrics["comment_count"], "3")
        self.assertEqual(metrics["share_count"], "2")
        self.assertEqual(metrics["impressions"], "100")
        self.assertEqual(metrics["reach"], "80")
        self.assertEqual(metrics["engaged_users"], "12")
        self.assertEqual(metrics["views"], "45")

    def test_iter_tool_post_metrics_returns_exportable_row(self):
        from unittest.mock import patch
        from django.utils import timezone

        credential = MetaCredential.objects.create(label="Test", access_token="token")
        fb = credential.accounts.create(platform="facebook", external_id="fb-metric", name="FB", access_token="page-token")
        target = PublishingTarget.objects.create(
            credential=credential,
            sync_key="fb:metric",
            display_name="Metric Target",
            facebook_account=fb,
        )
        target.post_logs.create(
            platform="facebook",
            scheduled_for=timezone.now(),
            published_at=timezone.now(),
            status="success",
            drive_file_id="file1",
            drive_file_name="POST1.jpeg",
            meta_creation_id="post-1",
        )

        with patch("scheduler.services.metrics.fetch_facebook_metrics", return_value={"permalink_url": "https://example.com/post-1", "reaction_count": "9"}):
            rows = iter_tool_post_metrics(target=target, days=7)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["post_id"], "post-1")
        self.assertEqual(rows[0]["permalink"], "https://example.com/post-1")

    def test_export_post_metrics_command_writes_csv_without_db_changes(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = f"{temp_dir}/metrics.csv"
            with patch(
                "scheduler.management.commands.export_post_metrics.iter_tool_post_metrics",
                return_value=[{"source": "tool", "platform": "facebook", "post_id": "post-1"}],
            ):
                call_command("export_post_metrics", "--output", output_path)

            with open(output_path, "r", encoding="utf-8") as handle:
                data = handle.read()

        self.assertIn("post-1", data)
