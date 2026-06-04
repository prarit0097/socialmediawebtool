from django.conf import settings
from django.db import migrations


def assign_admin_owner(apps, schema_editor):
    from django.conf import settings as dj_settings
    from django.contrib.auth.hashers import make_password

    User = apps.get_model("auth", "User")
    MetaCredential = apps.get_model("scheduler", "MetaCredential")
    PublishingTarget = apps.get_model("scheduler", "PublishingTarget")

    username = (getattr(dj_settings, "APP_ADMIN_USERNAME", "") or "admin").strip() or "admin"

    admin, created = User.objects.get_or_create(
        username=username,
        defaults={"is_staff": True, "is_superuser": True},
    )

    password = getattr(dj_settings, "APP_ADMIN_PASSWORD", "") or ""
    needs_save = False

    if created and password:
        admin.password = make_password(password)
        needs_save = True
    elif created and not password:
        print(
            f"[0008_assign_admin_owner] WARNING: admin user '{username}' created without a "
            f"usable password (APP_ADMIN_PASSWORD not set). Run "
            f"'manage.py changepassword {username}' before logging in."
        )

    if not admin.is_staff:
        admin.is_staff = True
        needs_save = True
    if not admin.is_superuser:
        admin.is_superuser = True
        needs_save = True

    if needs_save:
        admin.save()

    creds_assigned = MetaCredential.objects.filter(owner__isnull=True).update(owner=admin)
    targets_assigned = PublishingTarget.objects.filter(owner__isnull=True).update(owner=admin)

    print(
        f"[0008_assign_admin_owner] Assigned {creds_assigned} credential(s) and "
        f"{targets_assigned} target(s) to admin user '{username}'."
    )


class Migration(migrations.Migration):

    dependencies = [
        ("scheduler", "0007_metacredential_owner_publishingtarget_owner_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(assign_admin_owner, migrations.RunPython.noop),
    ]
