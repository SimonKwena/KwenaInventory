from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import Announcement, Notification, Transaction
from .permissions import ROLE_TEACHER, is_staff_role, role_label, role_of

CART_SESSION_KEY = "gearroom_cart"


def _cart_counts(request):
    raw = request.session.get(CART_SESSION_KEY, {})
    counts = {
        "book_ahead": len(raw.get("book_ahead", [])),
        "check_out": len(raw.get("check_out", [])),
    }
    counts["total"] = counts["book_ahead"] + counts["check_out"]
    return counts


def account_notifications(request):
    """Expose notification counts for the signed-in user's account menu."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "account_notification_count": 0,
            "unread_notification_count": 0,
            "user_notifications": [],
            "pending_request_count": 0,
            "user_role": "student",
            "user_role_label": "Student",
            "is_staff_role": False,
            "cart_counts": _cart_counts(request),
            "webpush_vapid_public_key": "",
        }
    now = timezone.now()
    announcement_count = (
        Announcement.objects.filter(is_active=True)
        .filter(Q(visible_until__isnull=True) | Q(visible_until__gte=now))
        .count()
    )
    unread_notification_count = Notification.objects.filter(user=user, is_read=False).count()
    user_notifications = list(
        Notification.objects.filter(user=user)
        .select_related("transaction")
        .order_by("-created_at")[:5]
    )
    pending_request_count = 0
    if user.is_staff:
        pending_request_count = Transaction.objects.filter(approval_status="pending", voided="none").count()
    return {
        "account_notification_count": announcement_count,
        "unread_notification_count": unread_notification_count,
        "user_notifications": user_notifications,
        "pending_request_count": pending_request_count,
        "user_role": role_of(user),
        "user_role_label": role_label(user),
        "is_teacher": role_of(user) == ROLE_TEACHER,
        "is_staff_role": is_staff_role(user),
        "cart_counts": _cart_counts(request),
        "webpush_vapid_public_key": getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", ""),
    }
