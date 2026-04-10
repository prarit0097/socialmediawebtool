from django.core.management.base import BaseCommand, CommandError

from scheduler.models import PublishingTarget
from scheduler.services.metrics import (
    enrich_manual_benchmark_rows,
    export_rows_to_csv,
    iter_tool_post_metrics,
    load_manual_benchmark_rows,
)


class Command(BaseCommand):
    help = "Export tool-post metrics and optional manual benchmark metrics to CSV without touching DB state."

    def add_arguments(self, parser):
        parser.add_argument("--target-id", type=int, help="Limit export to a single PublishingTarget id.")
        parser.add_argument("--days", type=int, default=7, help="How many recent days of successful tool posts to inspect.")
        parser.add_argument("--manual-csv", help="Optional CSV for manual benchmark posts. Columns: platform,post_id,target_id|sync_key,label,published_at")
        parser.add_argument("--output", required=True, help="Output CSV path.")

    def handle(self, *args, **options):
        target = None
        if options["target_id"]:
            target = PublishingTarget.objects.filter(pk=options["target_id"]).first()
            if target is None:
                raise CommandError("Target not found.")

        rows = iter_tool_post_metrics(target=target, days=options["days"])
        if options.get("manual_csv"):
            rows.extend(enrich_manual_benchmark_rows(load_manual_benchmark_rows(options["manual_csv"])))
        if not rows:
            raise CommandError("No rows available for export.")

        export_rows_to_csv(rows, options["output"])
        self.stdout.write(self.style.SUCCESS(f"Exported {len(rows)} row(s) to {options['output']}"))
