from datetime import time
import uuid

from django.db import models


class MetaCredential(models.Model):
    label = models.CharField(max_length=120)
    user_name = models.CharField(max_length=255, blank=True)
    user_id = models.CharField(max_length=100, blank=True)
    access_token = models.TextField()
    token_last_validated_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label", "-created_at"]

    def __str__(self):
        return self.label

    @property
    def masked_token(self):
        if len(self.access_token) <= 10:
            return "*" * len(self.access_token)
        return f"{self.access_token[:6]}...{self.access_token[-4:]}"


class SocialAccount(models.Model):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    PLATFORM_CHOICES = [
        (FACEBOOK, "Facebook"),
        (INSTAGRAM, "Instagram"),
    ]

    credential = models.ForeignKey(MetaCredential, on_delete=models.CASCADE, related_name="accounts")
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    external_id = models.CharField(max_length=100)
    name = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    access_token = models.TextField(blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("credential", "platform", "external_id")
        ordering = ["platform", "name", "username"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.name or self.username or self.external_id


class PublishingTarget(models.Model):
    credential = models.ForeignKey(MetaCredential, on_delete=models.CASCADE, related_name="targets")
    sync_key = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255)
    facebook_account = models.ForeignKey(
        SocialAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="facebook_targets",
        limit_choices_to={"platform": SocialAccount.FACEBOOK},
    )
    instagram_account = models.ForeignKey(
        SocialAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="instagram_targets",
        limit_choices_to={"platform": SocialAccount.INSTAGRAM},
    )
    drive_folder_id = models.CharField(max_length=255, blank=True)
    drive_folder_url = models.URLField(blank=True)
    posts_per_day = models.PositiveSmallIntegerField(default=1)
    posting_times = models.JSONField(default=list, blank=True)
    posting_window_start = models.TimeField(default=time(10, 0))
    posting_window_end = models.TimeField(default=time(18, 0))
    default_caption = models.TextField(blank=True)
    ai_enabled = models.BooleanField(default=False)
    ai_auto_caption_enabled = models.BooleanField(default=False)
    ai_language = models.CharField(max_length=50, default="Hinglish")
    ai_tone = models.CharField(max_length=50, default="Professional")
    ai_last_report_summary = models.TextField(blank=True)
    ai_last_generated_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    last_posted_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=50, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self):
        return self.display_name

    @property
    def is_connected_pair(self):
        return bool(self.facebook_account_id and self.instagram_account_id)


class PostLog(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    target = models.ForeignKey(PublishingTarget, on_delete=models.CASCADE, related_name="post_logs")
    platform = models.CharField(max_length=20, choices=SocialAccount.PLATFORM_CHOICES)
    scheduled_for = models.DateTimeField()
    published_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    drive_file_id = models.CharField(max_length=255, blank=True)
    drive_file_name = models.CharField(max_length=255, blank=True)
    meta_creation_id = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scheduled_for", "-created_at"]


class MediaAsset(models.Model):
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_PENDING = "pending"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    target = models.ForeignKey(PublishingTarget, on_delete=models.CASCADE, related_name="media_assets")
    drive_file_id = models.CharField(max_length=255)
    drive_file_name = models.CharField(max_length=255)
    variant = models.CharField(max_length=50, default="default")
    public_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    public_filename = models.CharField(max_length=255)
    local_path = models.CharField(max_length=500, blank=True)
    source_mime_type = models.CharField(max_length=120, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    last_error = models.TextField(blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("target", "drive_file_id", "variant")
        ordering = ["-updated_at"]


class AIMediaInsight(models.Model):
    target = models.ForeignKey(PublishingTarget, on_delete=models.CASCADE, related_name="ai_media_insights")
    drive_file_id = models.CharField(max_length=255)
    drive_file_name = models.CharField(max_length=255)
    source_mime_type = models.CharField(max_length=120, blank=True)
    primary_category = models.CharField(max_length=120, blank=True)
    secondary_tags = models.JSONField(default=list, blank=True)
    primary_caption = models.TextField(blank=True)
    hashtags = models.JSONField(default=list, blank=True)
    short_caption = models.TextField(blank=True)
    long_caption = models.TextField(blank=True)
    hindi_caption = models.TextField(blank=True)
    english_caption = models.TextField(blank=True)
    hinglish_caption = models.TextField(blank=True)
    duplicate_risk = models.CharField(max_length=20, blank=True)
    duplicate_reason = models.TextField(blank=True)
    quality_risk = models.CharField(max_length=20, blank=True)
    quality_issues = models.JSONField(default=list, blank=True)
    safe_to_post = models.BooleanField(default=True)
    translated_hindi = models.TextField(blank=True)
    translated_english = models.TextField(blank=True)
    translated_hinglish = models.TextField(blank=True)
    best_posting_times = models.JSONField(default=list, blank=True)
    best_posting_reason = models.TextField(blank=True)
    report_summary = models.TextField(blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    last_analyzed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("target", "drive_file_id")
        ordering = ["-updated_at"]


class DailyReportLog(models.Model):
    report_date = models.DateField(unique=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="pending")
    telegram_chat_id = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-report_date"]
