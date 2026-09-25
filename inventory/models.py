import json
import logging
import os
import urllib.request
import urllib.error
from datetime import timedelta
from io import BytesIO

import qrcode
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)


# Canonical status names. Look these up via get_status_option() rather than
# hardcoding strings across views, so a rename stays in one place.
STATUS_AVAILABLE = "Available"
STATUS_OUT = "Out"
STATUS_MAINTENANCE = "Maintenance"
STATUS_LATE_RETURN = "Late Return"

# Prefix for the short desk-facing request codes printed on slips (e.g. KW-0042).
REFERENCE_PREFIX = "KW-"


def get_status_option(name):
    return StatusOption.objects.filter(name=name).first()


def member_email(user):
    """Best available email for a user.

    Guests store their address on :class:`GuestProfile`, not on the auth
    ``User`` row, so fall back to that before giving up. Returns "" when the
    user has no usable address."""
    if not user:
        return ""
    email = getattr(user, "email", "") or ""
    if email:
        return email
    profile = getattr(user, "guest_profile", None)
    if profile and getattr(profile, "email", ""):
        return profile.email
    return ""


def display_name(user):
    """Best human-readable name for a user: guest full name, then the local
    account's full name, then the username as a last resort."""
    if not user:
        return ""
    profile = getattr(user, "guest_profile", None)
    if profile and profile.full_name:
        return profile.full_name
    full = user.get_full_name()
    if full:
        return full
    return user.username


def _lan_host():
    """Best-effort LAN IPv4 for this machine, used so QR codes point at an
    address a phone on the same network can actually reach (matching how the
    dev HTTPS server is accessed). Falls back to the configured Site domain."""
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


class ConditionOption(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class StatusOption(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Role(models.Model):
    """A location-visibility role. Mirrors the role slugs on
    :class:`UserProfile` so a location can be associated with several of them
    via the ``Location.roles`` many-to-many (each location visible to many
    roles, each role able to see many locations)."""

    slug = models.CharField(
        max_length=20,
        unique=True,
        choices=[
            ("superadmin", "Superadmin"),
            ("admin", "Admin"),
            ("staff", "Staff"),
            ("teacher", "Teacher"),
            ("student", "Student"),
            ("guest", "Guest"),
        ],
    )
    label = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.label or self.get_slug_display()

    def save(self, *args, **kwargs):
        if not self.label:
            self.label = self.get_slug_display()
        super().save(*args, **kwargs)


class LocationRole(models.Model):
    """Through table linking a :class:`Location` to a :class:`Role`. Each row
    grants the role visibility of that location."""

    location = models.ForeignKey("Location", on_delete=models.CASCADE, related_name="role_links")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="location_links")

    class Meta:
        unique_together = [("location", "role")]
        ordering = ["location__name", "role__slug"]

    def __str__(self):
        return f"{self.location.name} → {self.role}"


class LocationManager(models.Manager):
    """Order locations so every normal location is listed alphabetically
    first, and the "Room N" locations (Room 1 .. Room 10) come last
    in numeric order. Centralising it here means every dropdown and queryset
    that uses the default manager (including ModelChoiceField querysets)
    gets the same ordering without editing each call site."""

    # "Room N" entries are pinned to the end; everything else sorts first.
    PINNED_PREFIX = "Room "

    def get_queryset(self):
        from django.db.models import Case, When, Value, IntegerField

        whens = []
        for n in range(1, 11):
            whens.append(
                When(name=f"{self.PINNED_PREFIX}{n}", then=Value(n))
            )
        # Pinned "Room N" get keys 1..10 (after the 0 used by the rest),
        # so normal locations sort first and rooms trail in numeric order.
        order = Case(*whens, default=Value(0), output_field=IntegerField())
        return super().get_queryset().order_by(order, "name")


class Location(models.Model):
    objects = LocationManager()

    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    roles = models.ManyToManyField(
        Role,
        through="LocationRole",
        through_fields=("location", "role"),
        related_name="locations",
        blank=True,
        help_text="Roles that may see this location. Left empty, a location is visible to everyone.",
    )

    def __str__(self):
        return self.name


class Item(models.Model):
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100, blank=True)
    subcategory = models.CharField(max_length=100, blank=True)
    sku = models.CharField(max_length=100, blank=True, db_index=True)
    quantity_total = models.PositiveIntegerField(default=0)
    quantity_out = models.PositiveIntegerField(default=0)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="items")
    condition = models.ForeignKey(ConditionOption, on_delete=models.SET_NULL, blank=True, null=True, related_name="items")
    status = models.ForeignKey(StatusOption, on_delete=models.SET_NULL, blank=True, null=True, related_name="items")
    quantity_maintenance = models.PositiveIntegerField(default=0)
    image = models.ImageField(upload_to="items/", blank=True, null=True)
    qr_image = models.ImageField(upload_to="qr_codes/", blank=True, null=True)
    qr_code = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [
            # Every scan/lookup path resolves items by SKU with .get(sku__iexact=...),
            # which raises an unhandled error the moment two items share a code.
            # Blank SKUs are exempt (many items may legitimately have none yet).
            # NOTE: SQLite's UNIQUE is case-sensitive, so this catches exact
            # duplicates; the iexact lookups in views are an additional, belt-
            # and-braces guard against near-duplicates entered with different case.
            models.UniqueConstraint(
                fields=["sku"],
                condition=~models.Q(sku=""),
                name="unique_nonblank_item_sku",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def quantity_available(self):
        # Single source of truth: total minus what is currently out or in maintenance.
        return max(0, self.quantity_total - self.quantity_out - self.quantity_maintenance)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        expected_name = f"qr_codes/qr_{self.pk}.png"
        current = self.qr_image.name if self.qr_image else ""
        if current != expected_name:
            # Encode a host the scanner can reach: prefer the LAN IP (so a phone
            # on the same network opens the item), otherwise the Site domain.
            host = _lan_host() or Site.objects.get_current().domain
            qr_url = f"https://{host}/items/{self.pk}/"
            if self.sku:
                qr_url += f"?sku={self.sku}"
            qr_image = qrcode.make(qr_url)
            buffer = BytesIO()
            # qrcode.make() returns a qrcode image wrapper, not a raw PIL image;
            # .save on it writes a valid PNG to the buffer.
            qr_image.save(buffer, format="PNG")
            content = ContentFile(buffer.getvalue())

            storage = self.qr_image.storage
            # Remove any stale qr file for this item (old name or suffixed
            # duplicates) so we don't leak files or point at the wrong one.
            if current and storage.exists(current):
                storage.delete(current)
            if storage.exists(expected_name):
                storage.delete(expected_name)

            # Save under the stable, predictable name. storage.save respects the
            # given name when the file does not already exist, so we pre-delete
            # above to guarantee no random "_XXXX" suffix is appended.
            saved_name = storage.save(expected_name, content, max_length=255)
            self.qr_image.name = saved_name
            self.qr_code = self.qr_image.url
            super().save(update_fields=["qr_image", "qr_code"])


class UserProfile(models.Model):
    """Per-user profile holding the explicit ``role`` used for labelling and
    access control. Every user has one; the role is kept in sync with the
    Django ``is_staff`` / ``is_superuser`` flags via
    :func:`inventory.permissions.sync_role_flags`."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="user_profile")
    role = models.CharField(
        max_length=20,
        choices=[
            ("superadmin", "Superadmin"),
            ("admin", "Admin"),
            ("staff", "Staff"),
            ("teacher", "Teacher"),
            ("student", "Student"),
            ("guest", "Guest"),
        ],
        default="student",
    )
    has_seen_onboarding = models.BooleanField(
        default=False,
        help_text="Whether the user has dismissed the first-login onboarding popup.",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        name = self.user.username if self.user_id else "(unassigned)"
        return f"{name} — {self.get_role_display()}"


class GuestProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="guest_profile")
    full_name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    organization = models.CharField(max_length=150, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.full_name or self.user.username


class Announcement(models.Model):
    """Short update posted by an admin and shown to students on the home page."""

    STYLE_CHOICES = [
        ("info", "Info"),
        ("event", "Event"),
        ("alert", "Alert"),
        ("warning", "Warning"),
        ("maintenance", "Maintenance"),
        ("closed", "Closed"),
        ("success", "Good news"),
        ("reminder", "Reminder"),
    ]

    title = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    style = models.CharField(
        max_length=20,
        choices=STYLE_CHOICES,
        default="info",
        help_text="Changes the accent colour and icon shown with the update.",
    )
    pinned = models.BooleanField(
        default=False,
        help_text="Pinned updates stay at the top of the list and home panel.",
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="announcements"
    )
    created_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who posted the update.",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    visible_until = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-pinned", "-created_at"]

    def __str__(self):
        return self.title or (self.message[:40])

    def save(self, *args, **kwargs):
        if not self.created_by_name and self.created_by_id:
            self.created_by_name = display_name(self.created_by)
        super().save(*args, **kwargs)

    @property
    def is_visible(self):
        if not self.is_active:
            return False
        if self.visible_until and self.visible_until < timezone.now():
            return False
        return True


class Transaction(models.Model):
    TRANSACTION_TYPES = [
        ("book_ahead", "Book ahead"),
        ("check_out", "Check Out"),
        ("check_in", "Check In"),
    ]

    # Requests raised by non-admin users start as "pending" and do not move stock
    # until an admin approves them. Actions performed by an admin are approved
    # immediately.
    APPROVAL_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    # A voided transaction has been cancelled by an admin (wrong entry,
    # duplicate, etc.). The row is kept for audit, but excluded from stock math,
    # the member's gear list, and open requests.
    VOIDED_CHOICES = [
        ("none", "Not voided"),
        ("voided", "Voided"),
    ]

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="transactions")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="transactions")
    person_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the member the transaction is for.",
    )
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    quantity = models.PositiveIntegerField(default=1)
    condition = models.ForeignKey(ConditionOption, on_delete=models.SET_NULL, blank=True, null=True, related_name="transactions")
    status = models.ForeignKey(StatusOption, on_delete=models.SET_NULL, blank=True, null=True, related_name="transactions")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="transactions", blank=True, null=True)
    expected_return = models.DateTimeField(blank=True, null=True)
    taken_at = models.DateTimeField(blank=True, null=True, help_text="When the item is/was taken (book-ahead pickup).")
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default="approved")
    voided = models.CharField(max_length=20, choices=VOIDED_CHOICES, default="none")
    voided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="voided_transactions"
    )
    voided_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who voided the transaction.",
    )
    voided_at = models.DateTimeField(blank=True, null=True)
    voided_reason = models.TextField(blank=True, help_text="Why the transaction was voided.")

    decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="decided_transactions"
    )
    decided_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who decided the request.",
    )
    decided_at = models.DateTimeField(blank=True, null=True)
    # When this transaction belongs to a multi-item Request, the Request holds
    # the shared status/notes/dates/member. Legacy single-item transactions have
    # request=None and keep their own fields.
    request = models.ForeignKey(
        "inventory.Request", on_delete=models.CASCADE, related_name="transactions",
        blank=True, null=True,
    )
    # For a check-in, the check-out transaction it returns (so a returned unit
    # can be tied back to the exact loan / reservation it belongs to).
    source_transaction = models.ForeignKey(
        "self", on_delete=models.SET_NULL, related_name="returns",
        blank=True, null=True,
    )

    @property
    def is_pending(self):
        if self.request_id:
            return self.request.is_pending
        return self.approval_status == "pending"

    @property
    def is_voided(self):
        if self.request_id:
            return self.request.is_voided
        return self.voided == "voided"

    @property
    def approval_state(self):
        """The effective approval status, inheriting from the parent Request
        when one exists."""
        if self.request_id:
            return self.request.approval_status
        return self.approval_status

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.transaction_type} {self.quantity} x {self.item.name}"

    def save(self, *args, **kwargs):
        if not self.person_name and self.user_id:
            self.person_name = display_name(self.user)
        if not self.decided_by_name and self.decided_by_id:
            self.decided_by_name = display_name(self.decided_by)
        if not self.voided_by_name and self.voided_by_id:
            self.voided_by_name = display_name(self.voided_by)
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        return (
            self.expected_return is not None
            and self.expected_return < timezone.now()
            and self.transaction_type in {"check_out", "book_ahead"}
        )


class Request(models.Model):
    """A member's request that groups one or more items into a single unit of
    work needing ONE admin decision (approve/reject).

    A request carries the shared metadata (member, type, notes, pickup/return
    dates, approval status, void state). Each item in the request is a
    :class:`RequestItem`, and on approval each line materialises into its own
    :class:`Transaction` that actually moves stock (so a 3-item check-out still
    decrements each item's availability individually).
    """

    TRANSACTION_TYPES = Transaction.TRANSACTION_TYPES
    APPROVAL_CHOICES = Transaction.APPROVAL_CHOICES
    VOIDED_CHOICES = Transaction.VOIDED_CHOICES

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="requests")
    person_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the member the request is for.",
    )
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expected_return = models.DateTimeField(blank=True, null=True)
    taken_at = models.DateTimeField(blank=True, null=True, help_text="When the item is/was taken (book-ahead pickup).")
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default="pending")
    voided = models.CharField(max_length=20, choices=VOIDED_CHOICES, default="none")
    voided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="voided_requests"
    )
    voided_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who voided the request.",
    )
    voided_at = models.DateTimeField(blank=True, null=True)
    voided_reason = models.TextField(blank=True, help_text="Why the request was voided.")
    decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="decided_requests"
    )
    decided_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who decided the request.",
    )
    decided_at = models.DateTimeField(blank=True, null=True)
    handed_over_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="handed_over_requests"
    )
    handed_over_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who handed the gear over at pickup.",
    )
    handed_over_at = models.DateTimeField(blank=True, null=True)
    returned_at = models.DateTimeField(
        blank=True, null=True,
        help_text="When the handed-over gear was checked back in (auto-set from a check-in).",
    )
    returned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="returned_requests"
    )
    returned_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the admin who checked the gear back in.",
    )
    reference_code = models.CharField(
        max_length=20, blank=True, null=True,
        help_text="Short desk-facing code (e.g. KW-0042) printed on the slip so staff can link returns precisely. A return request reuses the code of the loan it closes.",
    )
    source_request = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="returns",
        help_text="For a check-in request, the loan (check-out / book-ahead) it returns gear against, so it shares that loan's slip code.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.transaction_type} request ({self.items.count()} item(s))"

    def save(self, *args, **kwargs):
        if not self.person_name and self.user_id:
            self.person_name = display_name(self.user)
        if not self.decided_by_name and self.decided_by_id:
            self.decided_by_name = display_name(self.decided_by)
        if not self.voided_by_name and self.voided_by_id:
            self.voided_by_name = display_name(self.voided_by)
        if not self.handed_over_by_name and self.handed_over_by_id:
            self.handed_over_by_name = display_name(self.handed_over_by)
        if not self.reference_code:
            self.reference_code = self.generate_reference_code()
        super().save(*args, **kwargs)

    @staticmethod
    def generate_reference_code():
        """Produce the next unique desk code, KW-#### (zero-padded counter).

        Reads the highest existing numeric suffix and increments, so codes stay
        short and human-friendly. Falls back to KW-0001 if none exist yet.
        """
        highest = 0
        for code in Request.objects.values_list("reference_code", flat=True):
            if code and code.startswith(REFERENCE_PREFIX):
                digits = code[len(REFERENCE_PREFIX):]
                if digits.isdigit():
                    highest = max(highest, int(digits))
        return f"{REFERENCE_PREFIX}{highest + 1:04d}"

    @property
    def is_pending(self):
        return self.approval_status == "pending"

    @property
    def is_voided(self):
        return self.voided == "voided"

    @property
    def is_handed_over(self):
        """True once an approved book-ahead reservation has been physically
        handed over at pickup (stock moved out)."""
        return self.handed_over_at is not None

    @property
    def is_returned(self):
        """True once the handed-over gear has been checked back in and the
        reservation is fully closed out."""
        return self.returned_at is not None

    @property
    def item_count(self):
        return self.items.count()

    @property
    def total_quantity(self):
        return sum(line.quantity for line in self.items.all())


class RequestItem(models.Model):
    """One line of a :class:`Request` — a single item with a quantity, optional
    pickup location and (for check-ins) condition. Materialises into a
    :class:`Transaction` when the parent request is approved."""

    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="request_items")
    quantity = models.PositiveIntegerField(default=1)
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, related_name="request_items", blank=True, null=True
    )
    condition = models.ForeignKey(
        ConditionOption, on_delete=models.SET_NULL, blank=True, null=True, related_name="request_items"
    )
    transaction = models.OneToOneField(
        Transaction, on_delete=models.SET_NULL, related_name="request_item",
        blank=True, null=True,
    )

    def __str__(self):
        return f"{self.quantity} x {self.item.name}"


class LiveVersion(models.Model):
    """Single-row counter bumped on every data change so open pages can detect
    when they need to refresh. DB-backed so it works across processes/workers."""

    version = models.BigIntegerField(default=0)

    class Meta:
        verbose_name = "Live version"

    def __str__(self):
        return f"version {self.version}"


class Maintenance(models.Model):
    """A record of an item sent for repair / servicing.

    An open record (no completed_at) means the item is currently out for
    maintenance; completing it returns the item to available stock.
    """

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="maintenance_records")
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, related_name="maintenance_records", blank=True, null=True,
        help_text="Where the item is while out for service (defaults to the item's location).",
    )
    reason = models.TextField(blank=True)
    reported_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="maintenance_reported"
    )
    reported_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the person who reported the item.",
    )
    started_at = models.DateTimeField(default=timezone.now)
    quantity = models.PositiveIntegerField(default=1)
    expected_return = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    completed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, blank=True, null=True, related_name="maintenance_completed"
    )
    completed_by_name = models.CharField(
        max_length=150, blank=True,
        help_text="Human-readable name of the person who returned the item.",
    )
    notes = models.TextField(blank=True)
    OUTCOME_CHOICES = [
        ("returned", "Returned / fixed"),
        ("written_off", "Could not be fixed"),
    ]
    outcome = models.CharField(
        max_length=20, choices=OUTCOME_CHOICES, blank=True,
        help_text="How the maintenance ended once the item came back (blank while open).",
    )

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Maintenance: {self.item.name}"

    def save(self, *args, **kwargs):
        if not self.reported_by_name and self.reported_by_id:
            self.reported_by_name = display_name(self.reported_by)
        if not self.completed_by_name and self.completed_by_id:
            self.completed_by_name = display_name(self.completed_by)
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.completed_at is None


class StockTake(models.Model):
    """A stock take event: a physical count of items at a location.

    ``status`` tracks the lifecycle:
    - ``draft`` — being prepared, items are being scanned/selected.
    - ``complete`` — the count is finalised and quantities have been
      adjusted to match the counted values.
    """

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("complete", "Complete"),
    ]

    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="stock_takes")
    taken_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="stock_takes")
    taken_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"Stock take {self.pk} — {self.location.name} ({self.get_status_display()})"

    @property
    def is_draft(self):
        return self.status == "draft"

    @property
    def is_complete(self):
        return self.status == "complete"

    @property
    def item_count(self):
        return self.items.count()

    def save(self, *args, **kwargs):
        if self.status == "complete" and self.completed_at is None:
            self.completed_at = timezone.now()
        super().save(*args, **kwargs)


class StockTakeItem(models.Model):
    """One line of a :class:`StockTake` — the counted quantity for a
    single item at the time of the stock take."""

    stock_take = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="items")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="stock_take_items")
    counted_quantity = models.PositiveIntegerField(default=0)
    expected_quantity = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["item__name"]
        unique_together = [("stock_take", "item")]

    def __str__(self):
        return f"{self.item.name}: counted {self.counted_quantity} (expected {self.expected_quantity})"

    @property
    def difference(self):
        return self.counted_quantity - self.expected_quantity

    def save(self, *args, **kwargs):
        # Keep expected_quantity in sync with the item's total at the time
        # of counting so discrepancies are meaningful even if the item
        # changes later.
        if self.item_id and not self.expected_quantity:
            self.expected_quantity = self.item.quantity_total
        super().save(*args, **kwargs)


class Notification(models.Model):
    """A personal notification for a single user, e.g. when an admin updates the
    status of a reservation or request. Distinct from Announcement, which is
    broadcast to every signed-in user.

    ``email_sent`` records whether the matching email was dispatched so we never
    double-send, and so a failed send can be retried.
    """

    CATEGORY_CHOICES = [
        ("reservation", "Reservation"),
        ("request", "Request"),
        ("general", "General"),
        ("reminder", "Reminder"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    transaction = models.ForeignKey(
        Transaction, on_delete=models.SET_NULL, blank=True, null=True, related_name="notifications"
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="general")
    title = models.CharField(max_length=200)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_read", "created_at"]),
        ]

    def __str__(self):
        return f"{self.category} notification for {self.user.username}: {self.title}"

    def send_email(self):
        """Email this notification to its user, if they have an address and an
        email backend is configured. Idempotent: a notification is only ever sent
        once. Returns True when an email was dispatched."""
        if self.email_sent:
            return False
        recipient = member_email(self.user)
        if not recipient:
            self.email_sent = True
            self.save(update_fields=["email_sent"])
            return False
        try:
            send_mail(
                subject=self.title,
                message=self.message,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[recipient],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to email notification %s to %s", self.pk, recipient)
            return False
        self.email_sent = True
        self.save(update_fields=["email_sent"])
        return True


class Reminder(models.Model):
    """Record that a reminder was sent for a transaction so we don't spam."""

    REMINDER_TYPES = [
        ("overdue", "Overdue"),
        ("due_soon", "Due soon"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reminders")
    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="reminders"
    )
    reminder_type = models.CharField(max_length=20, choices=REMINDER_TYPES)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [
            models.Index(fields=["user", "reminder_type", "sent_at"]),
        ]

    def __str__(self):
        return f"{self.reminder_type} reminder for {self.user.username} - {self.transaction}"


# How each (transaction_type, kind) maps to a notification. Book-aheads are
# reservations; check-out/in requests are categorised as "request".
_NOTIFY_KINDS = {
    "book_ahead": {
        "approved": ("reservation", "Reservation approved",
                     "Your reservation for {quantity} × {item} has been approved."),
        "rejected": ("reservation", "Reservation not approved",
                     "Your reservation for {quantity} × {item} was not approved."),
        "pending": ("reservation", "Reservation returned to pending",
                    "Your reservation for {quantity} × {item} was returned to pending for review."),
        "updated": ("reservation", "Reservation updated",
                    "Your reservation for {quantity} × {item} was updated by staff."),
        "voided": ("reservation", "Reservation voided",
                   "Your reservation for {quantity} × {item} was voided by staff."),
        "handed_over": ("pickup", "Gear picked up",
                        "Your reservation for {quantity} × {item} has been handed over — enjoy!"),
    },
    "check_out": {
        "approved": ("request", "Request approved",
                     "Your check-out request for {quantity} × {item} has been approved."),
        "rejected": ("request", "Request not approved",
                     "Your check-out request for {quantity} × {item} was not approved."),
        "pending": ("request", "Request returned to pending",
                    "Your check-out request for {quantity} × {item} was returned to pending for review."),
        "updated": ("request", "Request updated",
                    "Your check-out request for {quantity} × {item} was updated by staff."),
        "voided": ("request", "Request voided",
                   "Your check-out request for {quantity} × {item} was voided by staff."),
    },
    "check_in": {
        "approved": ("request", "Request approved",
                     "Your check-in request for {quantity} × {item} has been approved."),
        "rejected": ("request", "Request not approved",
                     "Your check-in request for {quantity} × {item} was not approved."),
        "pending": ("request", "Request returned to pending",
                     "Your check-in request for {quantity} × {item} was returned to pending for review."),
        "updated": ("request", "Request updated",
                     "Your check-in request for {quantity} × {item} was updated by staff."),
        "voided": ("request", "Request voided",
                    "Your check-in request for {quantity} × {item} was voided by staff."),
    },
}


def notify_transaction_change(txn, kind, *, actor_name=""):
    """Create a personal notification for the member describing how their request
    changed.

    Accepts either a :class:`Transaction` (legacy single-item) or a
    :class:`Request` (multi-item). For a Request, a single notification
    summarising every item is created, so the member gets one alert instead of
    one per line. ``kind`` is one of "approved", "rejected", "pending" or
    "updated". Returns the new Notification, or None when the kind is unknown.
    """
    from .models import Request  # noqa: local import to avoid cycle

    if isinstance(txn, Request):
        request_obj = txn
    else:
        request_obj = getattr(txn, "request", None)
    if request_obj is not None:
        target_user = request_obj.user
        transaction_type = request_obj.transaction_type
        summary = _summarize_request_items(request_obj)
        linked_transaction = None
    else:
        target_user = txn.user
        transaction_type = txn.transaction_type
        summary = f"{txn.quantity} x {txn.item.name if txn.item else 'item'}"
        linked_transaction = txn
    kind_map = _NOTIFY_KINDS.get(transaction_type)
    if not kind_map or kind not in kind_map:
        logger.debug("No notification template for %s/%s", transaction_type, kind)
        return None
    category, title_template, message_template = kind_map[kind]
    title = title_template
    message = message_template.format(quantity="", item=summary).strip()
    # Avoid a leading " x" when the template begins with the quantity slot.
    message = message.replace("  x", " ").replace(" x ", " ")
    if actor_name:
        message += f" (by {actor_name})"
    notification = Notification.objects.create(
        user=target_user,
        transaction=linked_transaction,
        category=category,
        title=title,
        message=message,
    )
    if getattr(settings, "EMAIL_BACKEND", None):
        notification.send_email()
    send_push_notification(target_user, title, message)
    return notification


def _summarize_request_items(request_obj):
    """Build a short human summary of a Request's items for notifications."""
    lines = []
    for line in request_obj.items.all():
        lines.append(f"{line.quantity} x {line.item.name}")
    if not lines:
        return "your request"
    if len(lines) == 1:
        return lines[0]
    if len(lines) <= 3:
        return ", ".join(lines)
    return f"{lines[0]}, {lines[1]} and {len(lines) - 2} more"


def notify_request_received(request_obj):
    """Email the member their desk slip when they submit a request.

    The slip carries the request's reference code (KW-####) — the shared
    reference the member shows at the counter and that admins look up in every
    manage tab. Best-effort: no-ops (and never crashes) when no email backend is
    configured or the member has no address. Returns the Notification or None.
    """
    if not getattr(settings, "EMAIL_BACKEND", None):
        return None
    summary = _summarize_request_items(request_obj)
    type_label = request_obj.transaction_type.replace("_", " ").title()
    title = f"Your {type_label} request — slip {request_obj.reference_code}"
    message = (
        f"Your {type_label} request for {summary} has been received and is awaiting "
        f"approval.\n\n"
        f"Your slip code is {request_obj.reference_code}. Show this code at the "
        f"counter when you collect or return the gear — staff use it to find your "
        f"request instantly.\n\n"
        f"An admin must approve the request before any stock moves."
    )
    notification = Notification.objects.create(
        user=request_obj.user,
        transaction=None,
        category="request",
        title=title,
        message=message,
    )
    notification.send_email()
    send_push_notification(request_obj.user, title, message)
    return notification


def cleanup_old_notifications(days=180, keep_unread=True):
    """Delete notifications older than ``days`` to bound table growth.

    When ``keep_unread`` is True, unread notifications are never deleted so a
    user can't "lose" an unseen alert. Returns the number of rows removed.
    """
    cutoff = timezone.now() - timedelta(days=days)
    qs = Notification.objects.filter(created_at__lt=cutoff)
    if keep_unread:
        qs = qs.filter(is_read=True)
    deleted, _ = qs.delete()
    return deleted or 0


class OneSignalPlayer(models.Model):
    """A OneSignal subscription ID linked to a Django user.

    OneSignal uses subscription IDs to identify specific browser/device instances
    for push delivery. A user may have multiple subscription IDs.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="onesignal_players")
    player_id = models.CharField(max_length=255, unique=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "last_used"]),
        ]

    def __str__(self):
        return f"OneSignal subscription for {self.user.username}"


def send_push_notification(user, title, message, url=""):
    """Send a push notification via OneSignal to the user's registered devices.

    Returns the number of notifications accepted by OneSignal.
    Failures are logged but never raised so notification delivery never
    interrupts the calling workflow.
    """
    app_id = getattr(settings, "ONESIGNAL_APP_ID", "")
    rest_key = getattr(settings, "ONESIGNAL_REST_API_KEY", "")
    if not app_id or not rest_key:
        return 0
    payload = json.dumps({
        "app_id": app_id,
        "include_external_user_ids": [str(user.pk)],
        "headings": {"en": title},
        "contents": {"en": message},
        "url": url or "/",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.onesignal.com/notifications",
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Key {rest_key}",
        },
        method="POST",
    )
    sent = 0
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                sent = 1
    except Exception:
        logger.exception("OneSignal push failed for user %s", user.pk)
    return sent
