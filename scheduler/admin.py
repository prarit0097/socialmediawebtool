from django.contrib import admin

from .models import AIMediaInsight, DailyReportLog, MediaAsset, MetaCredential, MetaCredentialEvent, PostLog, PublishingTarget, ScheduledPostRun, SocialAccount
from .services.credential_lifecycle import archive_credential, restore_credential


@admin.register(MetaCredential)
class MetaCredentialAdmin(admin.ModelAdmin):
    list_display = ("label", "owner", "user_name", "user_id", "is_active", "last_sync_at")
    list_filter = ("owner", "is_active")
    search_fields = ("label", "user_name", "user_id")
    actions = ["archive_credentials", "restore_credentials"]

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Archive selected credentials without deleting data")
    def archive_credentials(self, request, queryset):
        count = 0
        for credential in queryset:
            archive_credential(credential, source="django-admin", actor=str(request.user), note="Archived from Django admin.")
            count += 1
        self.message_user(request, f"{count} credential(s) archived. No rows were deleted.")

    @admin.action(description="Restore selected credentials")
    def restore_credentials(self, request, queryset):
        count = 0
        for credential in queryset:
            restore_credential(credential, source="django-admin", actor=str(request.user), note="Restored from Django admin.")
            count += 1
        self.message_user(request, f"{count} credential(s) restored.")


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ("display_name", "platform", "credential", "external_id", "is_active")
    list_filter = ("platform", "is_active")
    search_fields = ("name", "username", "external_id")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PublishingTarget)
class PublishingTargetAdmin(admin.ModelAdmin):
    list_display = ("display_name", "owner", "credential", "posts_per_day", "ai_enabled", "ai_auto_caption_enabled", "is_active", "last_status")
    list_filter = ("owner", "is_active", "ai_enabled", "ai_auto_caption_enabled")
    search_fields = ("display_name", "drive_folder_id")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PostLog)
class PostLogAdmin(admin.ModelAdmin):
    list_display = ("target", "platform", "scheduled_for", "status", "published_at")
    list_filter = ("platform", "status")
    search_fields = ("drive_file_name", "drive_file_id", "meta_creation_id")


@admin.register(ScheduledPostRun)
class ScheduledPostRunAdmin(admin.ModelAdmin):
    list_display = ("target", "scheduled_for", "status", "drive_file_name", "attempt_count", "next_retry_at")
    list_filter = ("status",)
    search_fields = ("drive_file_name", "drive_file_id", "last_error")


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("drive_file_name", "variant", "target", "status", "content_type", "file_size", "last_synced_at")
    list_filter = ("variant", "status", "content_type")
    search_fields = ("drive_file_name", "drive_file_id", "public_filename", "public_key")


@admin.register(AIMediaInsight)
class AIMediaInsightAdmin(admin.ModelAdmin):
    list_display = ("drive_file_name", "target", "primary_category", "duplicate_risk", "quality_risk", "last_analyzed_at")
    list_filter = ("primary_category", "duplicate_risk", "quality_risk", "safe_to_post")
    search_fields = ("drive_file_name", "drive_file_id", "primary_caption")


@admin.register(DailyReportLog)
class DailyReportLogAdmin(admin.ModelAdmin):
    list_display = ("report_date", "status", "sent_at", "telegram_chat_id")


@admin.register(MetaCredentialEvent)
class MetaCredentialEventAdmin(admin.ModelAdmin):
    list_display = ("credential_label", "credential", "action", "source", "actor", "created_at")
    list_filter = ("action", "source")
    search_fields = ("credential_label", "actor", "note")
    readonly_fields = ("credential", "credential_label", "action", "source", "actor", "note", "snapshot", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
