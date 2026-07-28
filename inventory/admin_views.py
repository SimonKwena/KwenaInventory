"""Read-only, staff-only admin page that shows the current Django settings in a
simple grouped layout. No secrets are exposed; only a curated, safe subset is
shown so admins can sanity-check the running configuration."""
from django.conf import settings as dj
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import User
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import UserAdminForm
from django.db.models import Q

from .models import Announcement, Maintenance, Request, Transaction, UserProfile

superuser_required = user_passes_test(lambda u: u.is_superuser)


_MASTER_LOG_TYPES = [
    ("transaction", "Transactions"),
    ("request", "Requests"),
    ("maintenance", "Maintenance"),
    ("announcement", "Updates"),
    ("user", "User accounts"),
]


def _build_master_log(event_type=None, query=None):
    """Return a single chronological list of activity entries aggregated from the
    various audit-bearing models. Each entry is a dict with ``when`` (datetime),
    ``who`` (display name or blank), ``type`` (slug from ``_MASTER_LOG_TYPES``),
    ``type_label``, ``action``, ``detail`` and an optional ``link``."""

    entries = []

    def _add(when, who, type_slug, type_label, action, detail, link=""):
        entries.append(
            {
                "when": when,
                "who": who or "",
                "type": type_slug,
                "type_label": type_label,
                "action": action,
                "detail": detail,
                "link": link,
            }
        )

    if event_type in (None, "transaction"):
        txns = (
            Transaction.objects.select_related("item", "user", "decided_by", "voided_by")
            .all()
            .order_by("-created_at")
        )
        if query:
            txns = txns.filter(
                Q(item__name__icontains=query)
                | Q(person_name__icontains=query)
                | Q(user__username__icontains=query)
                | Q(notes__icontains=query)
            )
        for t in txns:
            who = t.decided_by_name or t.person_name or (t.user.username if t.user else "")
            label = t.item.name if t.item else "item"
            detail = f"{t.get_transaction_type_display()} · {t.quantity} × {label}"
            if t.voided == "voided":
                detail += f" · voided by {t.voided_by_name or 'admin'}"
            if t.notes:
                detail += f" — {t.notes}"
            _add(t.created_at, who, "transaction", "Transaction", t.get_approval_status_display(), detail)

    if event_type in (None, "request"):
        reqs = (
            Request.objects.select_related("user", "decided_by", "voided_by")
            .all()
            .order_by("-created_at")
        )
        if query:
            reqs = reqs.filter(
                Q(person_name__icontains=query)
                | Q(user__username__icontains=query)
                | Q(notes__icontains=query)
            )
        for r in reqs:
            who = r.decided_by_name or r.person_name or (r.user.username if r.user else "")
            detail = f"{r.get_transaction_type_display()} · {r.total_quantity} item(s)"
            if r.voided == "voided":
                detail += f" · voided by {r.voided_by_name or 'admin'}"
            if r.notes:
                detail += f" — {r.notes}"
            _add(r.created_at, who, "request", "Request", r.get_approval_status_display(), detail)

    if event_type in (None, "maintenance"):
        maint = (
            Maintenance.objects.select_related("item", "reported_by", "completed_by")
            .all()
            .order_by("-started_at")
        )
        if query:
            maint = maint.filter(
                Q(item__name__icontains=query)
                | Q(reported_by_name__icontains=query)
                | Q(reason__icontains=query)
                | Q(notes__icontains=query)
            )
        for m in maint:
            action = "Returned" if m.completed_at else "Opened"
            who = m.completed_by_name or m.reported_by_name or ""
            detail = f"{m.item.name}" if m.item else "item"
            if m.reason:
                detail += f" — {m.reason}"
            if m.completed_at and m.outcome:
                detail += f" ({m.get_outcome_display()})"
            _add(m.started_at, who, "maintenance", "Maintenance", action, detail)

    if event_type in (None, "announcement"):
        anns = Announcement.objects.select_related("created_by").all().order_by("-created_at")
        if query:
            anns = anns.filter(
                Q(title__icontains=query)
                | Q(message__icontains=query)
                | Q(created_by_name__icontains=query)
            )
        for a in anns:
            state = "Active" if a.is_active else "Hidden"
            detail = a.title or (a.message[:60] if a.message else "")
            _add(a.created_at, a.created_by_name, "announcement", "Update", state, detail)

    if event_type in (None, "user"):
        profiles = (
            UserProfile.objects.select_related("user").all().order_by("-created_at")
        )
        if query:
            profiles = profiles.filter(
                Q(user__username__icontains=query)
                | Q(user__first_name__icontains=query)
                | Q(user__last_name__icontains=query)
                | Q(user__email__icontains=query)
                | Q(role__icontains=query)
            )
        for p in profiles:
            u = p.user
            detail = f"{u.get_full_name() or u.username} · role {p.get_role_display()}"
            if not u.is_active:
                detail += " (deactivated)"
            if u.is_superuser:
                detail += " · superuser"
            elif u.is_staff:
                detail += " · staff"
            _add(p.created_at, u.username, "user", "User account", "Account", detail)

    entries.sort(key=lambda e: e["when"], reverse=True)
    return entries


def _format(v):
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if v else "(empty)"
    if isinstance(v, dict):
        return ", ".join(f"{k}={vv}" for k, vv in v.items()) if v else "(empty)"
    if v is None or v == "":
        return "(not set)"
    return str(v)


@superuser_required
def site_settings_view(request):
    db = dj.DATABASES.get("default", {})
    groups = [
        ("General", [
            ("DEBUG", dj.DEBUG),
            ("TIME_ZONE", dj.TIME_ZONE),
            ("LANGUAGE_CODE", dj.LANGUAGE_CODE),
            ("USE_TZ", dj.USE_TZ),
            ("USE_I18N", dj.USE_I18N),
            ("SITE_ID", dj.SITE_ID),
        ]),
        ("Security / HTTPS", [
            ("USE_HTTPS", getattr(dj, "USE_HTTPS", False)),
            ("SESSION_COOKIE_SECURE", dj.SESSION_COOKIE_SECURE),
            ("CSRF_COOKIE_SECURE", dj.CSRF_COOKIE_SECURE),
            ("SECURE_HSTS_SECONDS", dj.SECURE_HSTS_SECONDS),
            ("ALLOWED_HOSTS", dj.ALLOWED_HOSTS),
            ("CSRF_TRUSTED_ORIGINS", dj.CSRF_TRUSTED_ORIGINS),
        ]),
        ("Database", [
            ("ENGINE", db.get("ENGINE")),
            ("NAME", db.get("NAME")),
        ]),
        ("Media & Static", [
            ("STATIC_URL", dj.STATIC_URL),
            ("MEDIA_URL", dj.MEDIA_URL),
            ("MEDIA_ROOT", dj.MEDIA_ROOT),
            ("STATICFILES_DIRS", dj.STATICFILES_DIRS),
        ]),
        ("Auth / Allauth", [
            ("ACCOUNT_EMAIL_VERIFICATION", dj.ACCOUNT_EMAIL_VERIFICATION),
            ("ACCOUNT_LOGIN_METHODS", dj.ACCOUNT_LOGIN_METHODS),
            ("ACCOUNT_SIGNUP_FIELDS", dj.ACCOUNT_SIGNUP_FIELDS),
            ("LOGIN_REDIRECT_URL", dj.LOGIN_REDIRECT_URL),
            ("LOGOUT_REDIRECT_URL", dj.LOGOUT_REDIRECT_URL),
        ]),
        ("Email", [
            ("EMAIL_BACKEND", dj.EMAIL_BACKEND),
            ("EMAIL_HOST", dj.EMAIL_HOST),
            ("EMAIL_PORT", dj.EMAIL_PORT),
            ("EMAIL_USE_TLS", dj.EMAIL_USE_TLS),
        ]),
        ("Installed Apps", [
            ("INSTALLED_APPS", dj.INSTALLED_APPS),
        ]),
    ]
    groups = [(title, [(k, _format(v)) for k, v in rows]) for title, rows in groups]
    return render(request, "admin/settings_view.html", {
        "title": "Django Settings",
        "groups": groups,
    })


@superuser_required
def users_list_view(request):
    users = User.objects.all().order_by("username")
    query = (request.GET.get("q") or "").strip()
    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
    return render(request, "admin/users.html", {
        "title": "Users",
        "users": users,
        "query": query,
    })


@superuser_required
def user_create_view(request):
    if request.method == "POST":
        form = UserAdminForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User '{user.username}' created.")
            return redirect("admin_users")
    else:
        form = UserAdminForm()
    return render(request, "admin/user_form.html", {
        "title": "Add user",
        "form": form,
        "is_new": True,
    })


@superuser_required
def user_edit_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = UserAdminForm(request.POST, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, f"User '{user.username}' updated.")
            return redirect("admin_users")
    else:
        form = UserAdminForm(instance=user)
    return render(request, "admin/user_form.html", {
        "title": f"Edit user: {user.username}",
        "form": form,
        "user_obj": user,
        "is_new": False,
    })


@superuser_required
@require_POST
def user_delete_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user.pk == request.user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("admin_users")
    username = user.username
    try:
        user.delete()
    except ProtectedError:
        messages.error(
            request,
            f"Cannot delete '{username}': they still have linked records "
            f"(transactions, maintenance, etc.). Deactivate the account instead.",
        )
        return redirect("admin_users")
    messages.success(request, f"User '{username}' deleted.")
    return redirect("admin_users")


@superuser_required
@require_POST
def user_toggle_active_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user.pk == request.user.pk:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("admin_users")
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    state = "active" if user.is_active else "inactive"
    messages.success(request, f"User '{user.username}' is now {state}.")
    return redirect("admin_users")


@superuser_required
def master_log_view(request):
    """Aggregated, chronological activity feed for superadmins spanning every
    audit-bearing model (transactions, requests, maintenance, updates and user
    accounts). The sidebar entry and this view are both superuser-only."""
    event_type = (request.GET.get("type") or "").strip() or None
    if event_type and event_type not in {slug for slug, _ in _MASTER_LOG_TYPES}:
        event_type = None
    query = (request.GET.get("q") or "").strip() or None

    entries = _build_master_log(event_type=event_type, query=query)

    return render(
        request,
        "admin/master_log.html",
        {
            "title": "Master Log",
            "entries": entries,
            "event_types": _MASTER_LOG_TYPES,
            "selected_type": event_type,
            "query": query or "",
            "total": len(entries),
        },
    )
