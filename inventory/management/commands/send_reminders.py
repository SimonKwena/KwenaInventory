import os

from django.core.management.base import BaseCommand
from django.db.models import Q, Sum
from django.utils import timezone
from datetime import timedelta

from inventory.models import Notification, Reminder, Request, Transaction, send_push_notification


def _active_requests_qs():
    """Return approved, not-returned, not-voided Requests with live gear out."""
    base = Request.objects.filter(
        voided="none",
        approval_status="approved",
        returned_at__isnull=True,
        source_request__isnull=True,
    ).filter(
        Q(transaction_type="check_out")
        | Q(transaction_type="book_ahead", handed_over_at__isnull=False)
    ).prefetch_related("items__item", "items__transaction")

    active = []
    for req in base:
        outstanding = 0
        for line in req.items.all():
            checkout = line.transaction
            if not checkout:
                continue
            returned = Transaction.objects.filter(
                source_transaction=checkout,
                transaction_type="check_in",
                approval_status="approved",
                voided="none",
            ).aggregate(total=Sum("quantity"))["total"] or 0
            if checkout.quantity - returned > 0:
                outstanding += checkout.quantity - returned
        if outstanding > 0:
            active.append(req)
    return active


def send_reminder_notifications(due_soon_hours=24, cooldown_hours=24):
    """Send reminders for active overdue and due-soon gear."""
    now = timezone.now()
    cooldown = timedelta(hours=cooldown_hours)

    sent = 0
    skipped = 0

    for req in _active_requests_qs():
        if not req.expected_return:
            continue

        first_line = req.items.first()
        txn = first_line.transaction if first_line else None
        user = req.user
        item_name = first_line.item.name if first_line else "your gear"
        qty = first_line.quantity if first_line else 1

        is_overdue = req.expected_return < now

        if is_overdue:
            reminder_type = "overdue"
            title = "Gear return overdue"
            message = (
                f"Your return for {qty} x {item_name} is overdue. "
                f"It was due on {timezone.localtime(req.expected_return).strftime('%d %b %Y, %H:%M')}. "
                "Please return it as soon as possible."
            )
        elif req.expected_return <= now + timedelta(hours=due_soon_hours):
            reminder_type = "due_soon"
            title = "Gear due soon"
            message = (
                f"Just a reminder: {qty} x {item_name} is due on "
                f"{timezone.localtime(req.expected_return).strftime('%d %b %Y, %H:%M')}."
            )
        else:
            continue

        recent = Reminder.objects.filter(
            user=user,
            transaction=txn,
            reminder_type=reminder_type,
            sent_at__gte=now - cooldown,
        ).exists()
        if recent:
            skipped += 1
            continue

        Notification.objects.create(
            user=user,
            transaction=txn,
            category="reminder",
            title=title,
            message=message,
        )
        send_push_notification(user, title, message)
        Reminder.objects.create(user=user, transaction=txn, reminder_type=reminder_type)
        sent += 1

    return sent, skipped


class Command(BaseCommand):
    help = "Send reminders for overdue and due-soon gear."

    def add_arguments(self, parser):
        parser.add_argument(
            "--due-soon-hours",
            type=int,
            default=24,
            help="Hours before expected return to send a due-soon reminder.",
        )
        parser.add_argument(
            "--reminder-cooldown",
            type=int,
            default=24,
            help="Minimum hours between reminders of the same type for the same transaction.",
        )

    def handle(self, *args, **options):
        sent, skipped = send_reminder_notifications(
            due_soon_hours=options["due_soon_hours"],
            cooldown_hours=options["reminder_cooldown"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Reminders sent: {sent}. Skipped (cooldown): {skipped}."
            )
        )