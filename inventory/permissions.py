"""Role definitions and helpers.

The app uses an explicit ``role`` per user (superadmin, admin, staff, teacher,
student, guest) for both labelling and access control. Roles are *derived from*
the existing Django ``is_staff`` / ``is_superuser`` flags plus a stored ``role``
field on :class:`UserProfile`, so legacy code that checks ``is_staff`` keeps
working unchanged.

Access hierarchy (most to least privileged):

* superadmin — ``is_superuser`` + ``is_staff``: every staff page, user
  management, and the raw settings/secrets view.
* admin      — ``is_staff`` (not superuser): every staff page, but no user
  management and no settings/secrets view.
* staff      — ``is_staff``: staff pages only.
* teacher / student / guest — not staff: member-facing pages only. ``guest``
  also carries a :class:`GuestProfile` with contact details.
"""

from django.contrib.auth.models import User
from django.db.models import Q as models_Q

# Canonical role slugs. Reference these instead of hardcoding strings.
ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_STAFF = "staff"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLE_GUEST = "guest"

ROLE_CHOICES = [
    (ROLE_SUPERADMIN, "Superadmin"),
    (ROLE_ADMIN, "Admin"),
    (ROLE_STAFF, "Staff"),
    (ROLE_TEACHER, "Teacher"),
    (ROLE_STUDENT, "Student"),
    (ROLE_GUEST, "Guest"),
]

ROLE_LABELS = dict(ROLE_CHOICES)

# Roles that count as "staff" (get access to staff-only pages).
STAFF_ROLES = {ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_STAFF}

DEFAULT_ROLE = ROLE_STUDENT


def role_of(user):
    """Return the user's role slug, deriving a sensible default for users who
    have no :class:`UserProfile` yet (legacy accounts)."""
    from .models import UserProfile

    if not user or not getattr(user, "is_authenticated", False):
        return DEFAULT_ROLE
    profile = getattr(user, "user_profile", None)
    if profile and profile.role:
        return profile.role
    # Derive from flags so the label is always correct even before backfill.
    if user.is_superuser:
        return ROLE_SUPERADMIN
    if user.is_staff:
        return ROLE_STAFF
    if getattr(user, "guest_profile", None) is not None:
        return ROLE_GUEST
    return DEFAULT_ROLE


def role_label(user):
    """Human-readable role label (e.g. "Superadmin")."""
    return ROLE_LABELS.get(role_of(user), DEFAULT_ROLE.title())


def is_superadmin(user):
    return bool(user and getattr(user, "is_authenticated", False) and user.is_superuser)


def is_admin_or_above(user):
    """Superadmin or admin. Used where the highest trust is needed but the raw
    settings/secrets view is not required."""
    return role_of(user) in {ROLE_SUPERADMIN, ROLE_ADMIN}


def is_staff_role(user):
    """True when the user's role grants staff-page access."""
    return role_of(user) in STAFF_ROLES


def sync_role_flags(user, role):
    """Keep ``is_staff`` / ``is_superuser`` consistent with a chosen ``role``
    so the existing flag-based checks keep working.

    Mapping:
      * superadmin -> is_staff=True, is_superuser=True
      * admin/staff -> is_staff=True, is_superuser=False
      * teacher/student/guest -> is_staff=False, is_superuser=False
    """
    if role == ROLE_SUPERADMIN:
        user.is_staff = True
        user.is_superuser = True
    elif role in {ROLE_ADMIN, ROLE_STAFF}:
        user.is_staff = True
        user.is_superuser = False
    else:
        user.is_staff = False
        user.is_superuser = False


def user_has_workspace_access(user):
    """True when the user may use the staff check-in/out workspace. This is the
    existing rule: staff members or anyone on the @kwenamusic.co.za domain."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    email = getattr(user, "email", "") or ""
    return user.is_staff or email.lower().endswith("@kwenamusic.co.za")


def user_has_active_gear(user):
    """True when the user currently has approved gear checked out (live
    check-outs not yet returned). Used to decide whether a member may check
    gear back in."""
    from .services import get_user_borrowed_items

    return bool(get_user_borrowed_items(user))


def can_book_ahead(user):
    """Anyone signed in may place a book-ahead reservation."""
    return bool(user and getattr(user, "is_authenticated", False))


def can_check_out(user):
    """Only workspace users (staff / @kwenamusic.co.za) may check gear out."""
    return user_has_workspace_access(user)


def can_check_in(user, has_active_gear=None):
    """Staff/admin roles may always check in. Members (students, teachers,
    guests) may check in only when they currently have gear out to return.

    A @kwenamusic.co.za email alone does not grant unconditional check-in:
    the staff/admin *role* does. This keeps the quick-action Check-in option
    hidden from students, teachers and guests who have no active loan."""
    if is_staff_role(user):
        return True
    if has_active_gear is None:
        has_active_gear = user_has_active_gear(user)
    return bool(has_active_gear)


def ensure_profile(user, role=None):
    """Create/update the user's :class:`UserProfile` so its stored ``role``
    matches ``role`` (or the flags). Returns the profile."""
    from .models import UserProfile

    profile, _ = UserProfile.objects.get_or_create(user=user)
    if role is None:
        role = role_of(user)
    if profile.role != role:
        profile.role = role
        profile.save(update_fields=["role"])
    return profile


def set_user_role(user, role):
    """Persist ``role`` on the user and sync the Django flags. Returns the user."""
    sync_role_flags(user, role)
    user.save()
    ensure_profile(user, role)
    return user


def visible_locations_for_role(role):
    """Return the base queryset of :class:`Location` rows a given role may see.

    A location with no ``roles`` links is considered visible to *everyone*, so
    it is always included. Superadmin additionally sees every location (this
    mirrors ``is_staff``/admin access: trusted staff can operate across all
    locations)."""
    from .models import Location

    if role in {ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_STAFF}:
        return Location.objects.filter(is_active=True)
    # Members: any location with no role restriction, plus locations that grant
    # their specific role.
    return Location.objects.filter(is_active=True).filter(
        models_Q(roles=None) | models_Q(roles__slug=role)
    ).distinct()


def visible_locations(user):
    """Visible locations for a user, based on their role. Superadmin/staff see
    all locations; members see only locations mapped to their role (and any
    unrestricted location)."""
    from .models import Location

    if not user or not getattr(user, "is_authenticated", False):
        return Location.objects.none()
    if is_staff_role(user):
        return Location.objects.filter(is_active=True)
    return visible_locations_for_role(role_of(user))


def location_is_visible(user, location):
    """True when ``location`` is visible to ``user``."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_staff_role(user):
        return location.is_active
    if not location.is_active:
        return False
    if not location.roles.exists():
        return True
    return location.roles.filter(slug=role_of(user)).exists()
