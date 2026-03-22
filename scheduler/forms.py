import json
from datetime import datetime

from django import forms

from .models import MetaCredential, PublishingTarget
from .services.drive import extract_drive_folder_id


class MetaCredentialForm(forms.ModelForm):
    class Meta:
        model = MetaCredential
        fields = ["label", "access_token"]
        widgets = {
            "label": forms.TextInput(attrs={"placeholder": "Primary Meta account"}),
            "access_token": forms.Textarea(attrs={"rows": 4, "placeholder": "Paste long-lived Meta access token"}),
        }


class PublishingTargetForm(forms.ModelForm):
    posting_times_json = forms.CharField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = PublishingTarget
        fields = [
            "drive_folder_url",
            "drive_folder_id",
            "posts_per_day",
            "posting_times_json",
            "posting_window_start",
            "posting_window_end",
            "default_caption",
            "is_active",
        ]
        widgets = {
            "drive_folder_url": forms.URLInput(attrs={"placeholder": "https://drive.google.com/drive/folders/..."}),
            "drive_folder_id": forms.TextInput(attrs={"placeholder": "Optional if URL pasted above"}),
            "posting_window_start": forms.TimeInput(attrs={"type": "time"}),
            "posting_window_end": forms.TimeInput(attrs={"type": "time"}),
            "default_caption": forms.Textarea(attrs={"rows": 4, "placeholder": "Optional default caption"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["posting_times_json"].initial = json.dumps(self.instance.posting_times or [])

    def clean(self):
        cleaned_data = super().clean()
        folder_url = cleaned_data.get("drive_folder_url", "").strip()
        folder_id = cleaned_data.get("drive_folder_id", "").strip()
        if folder_url and not folder_id:
            cleaned_data["drive_folder_id"] = extract_drive_folder_id(folder_url)
        if cleaned_data.get("posting_window_start") and cleaned_data.get("posting_window_end"):
            if cleaned_data["posting_window_start"] >= cleaned_data["posting_window_end"]:
                raise forms.ValidationError("Posting window end time must be later than start time.")
        if cleaned_data.get("posts_per_day", 0) < 1:
            raise forms.ValidationError("Posts per day must be at least 1.")

        raw_times = cleaned_data.get("posting_times_json", "").strip()
        parsed_times = []
        if raw_times:
            try:
                values = json.loads(raw_times)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(f"Invalid posting times payload: {exc}")
            if not isinstance(values, list):
                raise forms.ValidationError("Posting times payload must be a list.")
            for value in values:
                try:
                    parsed = datetime.strptime(value, "%H:%M").time()
                except ValueError:
                    raise forms.ValidationError(f"Invalid posting time: {value}")
                parsed_times.append(parsed.strftime("%H:%M"))
        if not parsed_times:
            raise forms.ValidationError("Please keep at least one posting time.")
        cleaned_data["posts_per_day"] = len(parsed_times)
        if len(set(parsed_times)) != len(parsed_times):
            raise forms.ValidationError("Posting times must be unique.")
        cleaned_data["posting_times"] = parsed_times
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.posting_times = self.cleaned_data.get("posting_times", [])
        if commit:
            instance.save()
        return instance
