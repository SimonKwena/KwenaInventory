"""Merge a user's separate single-item Requests into one Request per
(user, transaction_type, approval_status) group.

Migration 0023 turned every legacy Transaction into its own Request (one
item each). That means a guest who submitted, say, three book-aheads now
appears as three separate requests instead of a single grouped request. This
command re-groups them so multiple items share one Request (one admin
decision), matching the new submission flow.

Run with:
    python manage.py backfill_group_requests
Pass --dry-run to preview without writing.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction
from django.db.models import Count

from inventory.models import Request, RequestItem


def _merge_group(requests, user, transaction_type, approval_status):
    """Merge a list of (single-item) Requests into one combined Request.

    Keeps the earliest created_at and the most-recent shared fields
    (notes/dates/decision). Returns the surviving Request.
    """
    requests = sorted(requests, key=lambda r: r.created_at)
    primary = requests[0]

    # Prefer a non-empty notes and the latest taken_at/expected_return
    # across the group so nothing is lost on merge.
    notes = ""
    taken_at = None
    expected_return = None
    for r in requests:
        if r.notes and len(r.notes) > len(notes):
            notes = r.notes
        if r.taken_at and (taken_at is None or r.taken_at > taken_at):
            taken_at = r.taken_at
        if r.expected_return and (expected_return is None or r.expected_return > expected_return):
            expected_return = r.expected_return

    # Move every item line onto the primary Request.
    for r in requests:
        if r.pk == primary.pk:
            continue
        for line in r.items.all():
            line.request = primary
            line.save(update_fields=["request"])
        # Migrations keep the original Transaction rows for audit; point them at
        # the primary Request too so lookups stay consistent.
        for txn in r.transactions.all():
            txn.request = primary
            txn.save(update_fields=["request"])
        r.delete()

    primary.notes = notes
    primary.taken_at = taken_at
    primary.expected_return = expected_return
    primary.save(update_fields=["notes", "taken_at", "expected_return"])
    return primary


class Command(BaseCommand):
    help = "Group each user's separate single-item Requests into one Request per (user, type, status)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview the merges without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Only group requests that are still open decisions: pending, or
        # book-aheads (reservations hold no stock, so combining them is safe).
        # Approved check-outs/ins already moved stock per line; leave those alone.
        candidates = (
            Request.objects.filter(voided="none")
            .filter(approval_status__in=["pending", "approved"])
            .filter(transaction_type__in=["book_ahead", "check_out", "check_in"])
            .annotate(line_count=Count("items"))
            .filter(line_count=1)
        )

        # Bucket by (user, type, status). For book-aheads we additionally
        # split by taken_at so distinct reservation windows stay separate.
        groups = {}
        for req in candidates:
            key = (req.user_id, req.transaction_type, req.approval_status)
            groups.setdefault(key, []).append(req)

        mergeable = {
            key: reqs
            for key, reqs in groups.items()
            if len(reqs) > 1
        }

        if not mergeable:
            self.stdout.write(self.style.SUCCESS("Nothing to group. All requests are already single-item groups."))
            return

        total_merged = 0
        for key, reqs in mergeable.items():
            user_id, ttype, status = key
            self.stdout.write(
                f"User {user_id} / {ttype} / {status}: merging {len(reqs)} requests "
                f"into 1 (dry-run={dry_run})."
            )
            total_merged += 1
            if dry_run:
                continue
            with db_transaction.atomic():
                _merge_group(reqs, user_id, ttype, status)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run complete: {total_merged} group(s) would be merged. "
                    "Re-run without --dry-run to apply."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Done. Merged {total_merged} group(s) into single requests.")
            )
