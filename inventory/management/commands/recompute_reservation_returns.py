"""Recompute ``returned_at`` for live loans whose gear is already back.

A loan (a direct check-out, or a handed-over book-ahead) becomes "Returned"
once the gear it released is checked back in. Loans returned via the child
return-request flow (member self-return, or the admin quick-action check-in) or
older reservations whose hand-over predates the automatic link may be stuck on
"Approved"/"Collected" even though the gear is already back. This command walks
every approved, not-yet-returned loan and marks it returned when its check-outs
are fully closed out, using the same logic as a live check-in.

Usage::

    python manage.py recompute_reservation_returns          # fix and report
    python manage.py recompute_reservation_returns --check  # report only, no writes
"""
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone

from inventory.models import Request, Transaction


def _checkout_returned(checkout):
    """True when every unit of this check-out has a matching approved check-in."""
    returned = (
        Transaction.objects.filter(
            source_transaction=checkout,
            transaction_type="check_in",
            approval_status="approved",
            voided="none",
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    return returned >= checkout.quantity


def _loan_returned_meta(loan):
    """Best-guess (returned_at, returned_by, returned_by_name) for a fully
    returned loan, taken from the latest check-in that closed it."""
    checkout_ids = [l.transaction_id for l in loan.items.all() if l.transaction_id]
    last = None
    if checkout_ids:
        last = (
            Transaction.objects.filter(
                source_transaction_id__in=checkout_ids,
                transaction_type="check_in",
                approval_status="approved",
                voided="none",
            )
            .order_by("created_at")
            .last()
        )
    if last is not None:
        return last.created_at, last.decided_by, (last.decided_by_name or "")
    return (loan.handed_over_at or timezone.now()), None, ""


class Command(BaseCommand):
    help = "Mark live loans (check-outs and handed-over book-aheads) returned once their gear is back."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Only report loans that should be marked returned; do not write.",
        )

    def handle(self, *args, **options):
        check_only = options["check"]
        fixed = 0

        open_loans = (
            Request.objects.filter(
                voided="none",
                approval_status="approved",
                returned_at__isnull=True,
                source_request__isnull=True,
            )
            .filter(
                Q(transaction_type="check_out")
                | Q(transaction_type="book_ahead", handed_over_at__isnull=False)
            )
            .prefetch_related("items__transaction", "items__item")
        )

        for loan in open_loans:
            fully_returned = True
            has_checkout = False
            for line in loan.items.all():
                checkout = line.transaction
                if checkout is not None:
                    has_checkout = True
                    if not _checkout_returned(checkout):
                        fully_returned = False
                        break
                    continue
                # Legacy line with no linked check-out: fall back to counting the
                # member's check-ins for the item since hand-over.
                if line.item_id is None or loan.handed_over_at is None:
                    fully_returned = False
                    break
                in_qty = (
                    Transaction.objects.filter(
                        item_id=line.item_id,
                        transaction_type="check_in",
                        approval_status="approved",
                        voided="none",
                        user=loan.user,
                        created_at__gte=loan.handed_over_at,
                    ).count()
                )
                if in_qty < line.quantity:
                    fully_returned = False
                    break

            # Never mark a loan returned when it has no evidence of any check-out
            # at all (e.g. a book-ahead with only legacy lines and no hand-over).
            if not has_checkout and loan.transaction_type == "check_out":
                fully_returned = False

            if fully_returned:
                fixed += 1
                who = loan.person_name or loan.user.username
                code = loan.reference_code or f"pk-{loan.pk}"
                if check_only:
                    self.stdout.write(
                        self.style.WARNING(f"WOULD MARK returned: {code} ({who})")
                    )
                else:
                    when, by, by_name = _loan_returned_meta(loan)
                    with db_transaction.atomic():
                        loan.returned_at = when
                        loan.returned_by = by
                        loan.returned_by_name = by_name
                        loan.save(
                            update_fields=[
                                "returned_at",
                                "returned_by",
                                "returned_by_name",
                            ]
                        )
                    self.stdout.write(
                        self.style.SUCCESS(f"MARKED returned: {code} ({who})")
                    )

        if fixed == 0:
            self.stdout.write(self.style.SUCCESS("No loans need updating. Nothing to do."))
        elif check_only:
            self.stdout.write(
                self.style.WARNING(f"{fixed} loan(s) would be marked returned (--check used).")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Marked {fixed} loan(s) as returned."))
