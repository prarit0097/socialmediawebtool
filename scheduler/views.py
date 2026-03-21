from pathlib import Path

from django.http import FileResponse, HttpResponse
from django.core.signing import BadSignature, SignatureExpired
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt

from .forms import MetaCredentialForm, PublishingTargetForm
from .models import MediaAsset, MetaCredential, PublishingTarget
from .services.drive import download_drive_file, get_drive_file_metadata
from .services.health import build_target_health
from .services.media_transform import build_instagram_ready_image
from .services.meta import MetaAPIError, sync_credential_accounts
from .services.publishing import PublishingError, publish_due_targets, publish_target_now
from .services.proxy import unsign_media_token
from .services.telegram import send_daily_report


@require_http_methods(["GET", "POST"])
def dashboard(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_token":
            form = MetaCredentialForm(request.POST)
            if form.is_valid():
                credential = form.save()
                try:
                    sync_credential_accounts(credential)
                    messages.success(request, "Meta token saved and accounts synced.")
                except MetaAPIError as exc:
                    messages.error(request, f"Token saved, but sync failed: {exc}")
                return redirect("scheduler:dashboard")
        elif action == "sync_credential":
            credential = get_object_or_404(MetaCredential, pk=request.POST.get("credential_id"))
            try:
                sync_credential_accounts(credential)
                messages.success(request, f"{credential.label} synced.")
            except MetaAPIError as exc:
                messages.error(request, f"Sync failed: {exc}")
            return redirect("scheduler:dashboard")
        elif action == "delete_credential":
            credential = get_object_or_404(MetaCredential, pk=request.POST.get("credential_id"))
            label = credential.label
            credential.delete()
            messages.success(request, f"{label} deleted permanently.")
            return redirect("scheduler:dashboard")
        elif action == "run_due_posts":
            result = publish_due_targets()
            messages.success(request, f"Posting run completed. Success={result['success']} Failed={result['failed']}")
            return redirect("scheduler:dashboard")
        elif action == "send_report":
            report = send_daily_report()
            messages.success(request, report["status_message"])
            return redirect("scheduler:dashboard")
    else:
        form = MetaCredentialForm()

    targets = PublishingTarget.objects.select_related("facebook_account", "instagram_account", "credential")
    context = {
        "form": form,
        "credentials": MetaCredential.objects.prefetch_related("targets").all(),
        "connected_targets": [{"target": target, "health": build_target_health(target)} for target in targets if target.is_connected_pair],
        "unconnected_targets": [{"target": target, "health": build_target_health(target)} for target in targets if not target.is_connected_pair],
    }
    return render(request, "scheduler/dashboard.html", context)


@require_http_methods(["GET", "POST"])
def target_detail(request, pk):
    target = get_object_or_404(
        PublishingTarget.objects.select_related("facebook_account", "instagram_account", "credential"),
        pk=pk,
    )
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "test_post":
            try:
                publish_target_now(target)
                target.refresh_from_db(fields=["last_status", "last_error"])
                if target.last_status == "success":
                    messages.success(request, "Test post completed successfully on all active platforms.")
                else:
                    messages.warning(request, f"Test post ran, but some platform still needs attention: {target.last_error}")
            except Exception as exc:
                messages.error(request, f"Test post failed: {exc}")
            return redirect("scheduler:target_detail", pk=target.pk)
        form = PublishingTargetForm(request.POST, instance=target)
        if form.is_valid():
            form.save()
            messages.success(request, "Publishing target updated.")
            return redirect("scheduler:target_detail", pk=target.pk)
    else:
        form = PublishingTargetForm(instance=target)
    context = {
        "target": target,
        "form": form,
        "health": build_target_health(target),
    }
    return render(request, "scheduler/target_detail.html", context)


@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def media_proxy(request, token, filename):
    try:
        payload = unsign_media_token(token)
    except (BadSignature, SignatureExpired):
        return HttpResponse("Invalid media token.", status=404)
    target = get_object_or_404(PublishingTarget, pk=payload["target_id"])
    if not target.is_active:
        return HttpResponse("Target inactive.", status=404)

    metadata = get_drive_file_metadata(payload["file_id"])
    variant = request.GET.get("variant", "")
    content_type = metadata.get("mimeType", "application/octet-stream")
    body = b""
    file_name = metadata.get("name", "media")

    should_transform_image = variant == "instagram_image" and content_type.startswith("image/")
    if should_transform_image or request.method != "HEAD":
        body = download_drive_file(payload["file_id"])
        if should_transform_image:
            body = build_instagram_ready_image(body)
            content_type = "image/jpeg"
            if "." in file_name:
                file_name = file_name.rsplit(".", 1)[0] + ".jpg"
            else:
                file_name += ".jpg"

    response = HttpResponse(body if request.method != "HEAD" else b"", content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{file_name}"'
    if body:
        response["Content-Length"] = str(len(body))
    elif metadata.get("size"):
        response["Content-Length"] = metadata["size"]
    return response


@require_http_methods(["GET", "HEAD"])
def public_media(request, public_key, filename):
    asset = get_object_or_404(MediaAsset, public_key=public_key, status=MediaAsset.STATUS_READY)
    path = Path(asset.local_path)
    if not path.exists():
        return HttpResponse("Cached media file missing.", status=404)
    response = FileResponse(open(path, "rb"), content_type=asset.content_type or "application/octet-stream")
    response["Content-Disposition"] = f'inline; filename="{asset.public_filename}"'
    response["Content-Length"] = str(asset.file_size)
    return response
