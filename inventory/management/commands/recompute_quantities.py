"""Recompute each item's ``quantity_out`` (and derived ``status``) from its
approved, non-voided transactions.

The dashboard and home pages read the stored ``quantity_out`` field directly, so
when that field drifts out of sync with the transaction history those pages show
stale/incorrect stock numbers. This command rebuilds ``quantity_out`` from the
source of truth (the Transactions) and reports any item it had to correct.

Usage::

    python manage.py recompute_quantities          # fix and report
    python manage.py recompute_quantities --check  # report only, no writes
"""
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction

from inventory.models import StockEntry
from inventory.services import _resolve_status


def derived_quantity_out(item):
    """Recompute the live "out" count for an item from its transactions."""
    out = 0
    for txn in item.transactions.filter(approval_status="approved", voided="none"):
        if txn.transaction_type == "check_out":
            out += txn.quantity
        elif txn.transaction_type == "check_in":
            out -= txn.quantity
    return max(0, out)


class Command(BaseCommand):
    help = "Recompute stock entry quantity_out and status from the transaction history."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Only report drift; do not write changes to the database.",
        )

    def handle(self, *args, **options):
        check_only = options["check"]
        fixed = 0
        for item in StockEntry.objects.all().select_related("status"):
            fresh = derived_quantity_out(item)
            if fresh == item.quantity_out and item.status == _resolve_status(item):
                continue
            old_out = item.quantity_out
            old_status = item.status.name if item.status else None
            item.quantity_out = fresh
            item.status = _resolve_status(item)
            if not check_only:
                with db_transaction.atomic():
                    item.save(update_fields=["quantity_out", "status"])
            fixed += 1
            self.stdout.write(
                self.style.WARNING(
                    f"{'WOULD FIX' if check_only else 'FIXED'} "
                    f"stock entry {item.pk} ({item.name!r}): "
                    f"quantity_out {old_out} -> {fresh}, "
                    f"status {old_status} -> "
                    f"{item.status.name if item.status else None}"
                )
            )
        if fixed == 0:
            self.stdout.write(self.style.SUCCESS("All stock entries consistent. Nothing to do."))
        elif check_only:
            self.stdout.write(
                self.style.WARNING(f"{fixed} stock entry(ies) out of sync (not modified, --check used).")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Recomputed and corrected {fixed} stock entry(ies)."))
