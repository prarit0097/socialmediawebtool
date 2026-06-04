from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a login user for the app (optionally a superuser)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Username for the new user.")
        parser.add_argument("--password", required=True, help="Password for the new user.")
        parser.add_argument("--email", default="", help="Email address for the new user.")
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Create the user as a superuser.",
        )

    def handle(self, *args, **options):
        username = options["username"]
        password = options["password"]
        email = options["email"]
        superuser = options["superuser"]

        User = get_user_model()

        if User.objects.filter(username=username).exists():
            raise CommandError(f"User '{username}' already exists.")

        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("Invalid password: " + "; ".join(exc.messages))

        if superuser:
            User.objects.create_superuser(username=username, email=email, password=password)
        else:
            User.objects.create_user(username=username, email=email, password=password)

        self.stdout.write(
            self.style.SUCCESS(f"Created {'superuser' if superuser else 'user'} '{username}'.")
        )
