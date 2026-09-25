import json
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Q

from django.http import FileResponse, JsonResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from .forms import (
    AnnouncementForm,
    CatalogItemBasicForm,
    GuestLoginForm,
    GuestProfileForm,
    ItemForm,
    LocalAccountForm,
    MaintenanceForm,
    RequestEditForm,
    ScanItemForm,
    StockEntryFormSet,
    StockEntryRowForm,
    TransactionForm,
)
from .models import (
    Announcement,
    CatalogItem,
    ConditionOption,
    GuestProfile,
    Location,
    Maintenance,
    Notification,
    OneSignalPlayer,
    Request,
    RequestItem,
    StockEntry,
    StockTake,
    StockTakeItem,
    Transaction,
    display_name,
    notify_request_received,
    notify_transaction_change,
)
from .services import (
    _localize,
    apply_request,
    complete_item_maintenance,
    create_request,
    get_user_active_loans,
    get_user_borrowed_items,
    get_user_gear_summary,
    get_user_request_history,
    hand_over_request,
    record_item_transaction,
    return_request_by_code,
    reject_request,
    set_item_maintenance,
    void_request,
    void_transaction,
    write_off_maintenance,
)
from .permissions import (
    ROLE_TEACHER,
    can_book_ahead,
    can_check_in,
    can_check_out,
    ensure_profile,
    is_admin_or_above,
    is_staff_role,
    is_superadmin,
    location_is_visible,
    role_of,
    user_has_workspace_access,
    visible_locations,
)


def landing(request):
    return render(request, "inventory/landing.html")


def item_lookup(request):
    sku = (request.GET.get("sku") or "").strip()
    pk = (request.GET.get("pk") or "").strip()
    stock_entry = None
    if pk:
        stock_entry = StockEntry.objects.filter(pk=pk, is_active=True).select_related("catalog_item", "location").first()
    elif sku:
        stock_entry = (
            StockEntry.objects.filter(catalog_item__sku__iexact=sku, is_active=True)
            .select_related("catalog_item", "location")
            .first()
        )
    if stock_entry and not request.user.is_staff and not location_is_visible(request.user, stock_entry.location):
        stock_entry = None
    if not stock_entry:
        return JsonResponse({"found": False})
    return JsonResponse({
        "found": True,
        "id": stock_entry.pk,
        "sku": stock_entry.catalog_item.sku or "",
        "name": stock_entry.name,
        "location_id": stock_entry.location_id,
    })


CART_SESSION_KEY = "gearroom_cart"
ITEM_CREATE_DRAFT_SESSION_KEY = "item_create_draft"


def _cart_items(request):
    """Return the user's session cart as a list of dicts, each representing one
    action group containing its selected items."""
    raw = request.session.get(CART_SESSION_KEY, {})
    actions = [("book_ahead", "Book ahead"), ("check_out", "Check out")]
    items = []
    for action, label in actions:
        entries = raw.get(action, [])
        if not entries:
            continue
        resolved = []
        for entry in entries:
            stock_entry = StockEntry.objects.filter(pk=entry.get("item_id"), is_active=True).select_related("catalog_item", "location").first()
            if not stock_entry:
                continue
            resolved.append(
                {
                    "item_id": stock_entry.pk,
                    "name": stock_entry.name,
                    "location": stock_entry.location.name if stock_entry.location else "",
                    "location_id": stock_entry.location.pk if stock_entry.location else "",
                    "quantity": max(1, int(entry.get("quantity") or 1)),
                    "quantity_available": stock_entry.quantity_available,
                }
            )
        if resolved:
            items.append({"action": action, "label": label, "entries": resolved})
    return items


def _cart_counts(request):
    raw = request.session.get(CART_SESSION_KEY, {})
    counts = {"book_ahead": len(raw.get("book_ahead", [])), "check_out": len(raw.get("check_out", []))}
    counts["total"] = counts["book_ahead"] + counts["check_out"]
    return counts


def _pop_slip_request(request):
    """One-shot load of the request whose slip should pop up on the member's
    home page. The id is stashed in the session by ``process_transaction`` /
    ``scan_item`` right after a successful submission and consumed (popped) here
    so a later refresh does not re-open the popup. Only returns a request owned
    by the current user."""
    from .models import Request

    if not request.user.is_authenticated:
        return None
    slip_id = request.session.pop(SLIP_SESSION_KEY, None)
    if not slip_id:
        return None
    try:
        return (
            Request.objects.filter(pk=int(slip_id), user=request.user, voided="none")
            .select_related("user", "user__user_profile", "user__guest_profile", "decided_by")
            .prefetch_related(
                "items__item", "items__location", "items__condition",
                "returns", "returns__items__item", "returns__items__condition",
            )
            .first()
        )
    except (ValueError, TypeError):
        return None


def _slip_return_url(request, slip_request):
    """Absolute URL a phone can scan to pre-fill the return form for a slip."""
    if not slip_request or not slip_request.reference_code:
        return ""
    return request.build_absolute_uri(
        f"{reverse('inventory:return_by_code')}?code={slip_request.reference_code}"
    )


def _resolve_checkin_source(user, item, submitted_id, as_staff=False):
    """Work out which loan a check-in should be filed against.

    Prefers an explicit choice from the member (their own active loan, picked
    by slip code on the form). This makes returns exact instead of guessed.
    Falls back to auto-detection only when the member did not pick and has a
    single outstanding loan for the item; with several loans we stay silent so
    the form can ask rather than guess wrong.

    When ``as_staff`` is True (an admin checking gear in at the counter on
    behalf of a member), the explicit ``submitted_id`` is resolved without the
    owning-user filter, so staff can return any member's loan by its pk."""
    from .models import Request

    if submitted_id:
        try:
            q = Request.objects.filter(
                pk=int(submitted_id),
                voided="none",
                approval_status="approved",
                returned_at__isnull=True,
                source_request__isnull=True,
            )
            if not as_staff:
                q = q.filter(user=user)
            chosen = q.first()
        except (ValueError, TypeError):
            chosen = None
        if chosen and chosen.items.filter(item=item).exists():
            return chosen
        return None
    # No explicit choice: auto-detect only when unambiguous (one loan).
    loans = get_user_active_loans(user)
    matching = [entry for entry in loans if any(li["item"] == item for li in entry["items"])]
    if len(matching) == 1:
        return matching[0]["loan"]
    return None


# Session key under which a freshly submitted request's pk is stashed so the
# home page can show its slip as a popup.
SLIP_SESSION_KEY = "slip_request_id"

def _summarize_gear_request(req):
    """Small display dict for a user's own request, for the Home info panel."""
    from .models import _summarize_request_items

    label = req.transaction_type.replace("_", " ").title()
    lines = [_summarize_request_items(req)]
    summary = f"{lines[0]} ({label})"
    return {
        "req": req,
        "item": req.items.first().item if req.items.exists() else None,
        "quantity": req.total_quantity,
        "summary": summary,
        "approval_status": req.approval_status,
    }


def _home_pending_requests(user):
    """Requests awaiting admin approval: pending check-outs/ins plus book-aheads
    that have not been approved yet."""
    from .models import Request

    return [
        _summarize_gear_request(req)
        for req in (
            Request.objects.filter(user=user, voided="none", approval_status="pending")
            .exclude(transaction_type="book_ahead", approval_status="approved")
            .prefetch_related("items__item")
            .order_by("-created_at")
        )
    ]


def _home_reservations(user):
    """Approved book-ahead reservations still held until pickup. Handed-over
    reservations have already become live check-outs, so they are excluded."""
    from .models import Request

    return [
        _summarize_gear_request(req)
        for req in (
            Request.objects.filter(
                user=user,
                voided="none",
                approval_status="approved",
                transaction_type="book_ahead",
                handed_over_at__isnull=True,
            )
            .prefetch_related("items__item")
            .order_by("-created_at")
        )
    ]


@csrf_exempt
def cart_add(request):
    if not request.user.is_authenticated:
        return redirect("inventory:catalog")
    action = request.POST.get("action", "")
    if action not in ("book_ahead", "check_out"):
        messages.error(request, "Choose a valid action to add to your cart.")
        return redirect("inventory:catalog")
    try:
        item_id = int(request.POST.get("item_id"))
    except (TypeError, ValueError):
        messages.error(request, "That item could not be found.")
        return redirect("inventory:catalog")
    if not StockEntry.objects.filter(pk=item_id, is_active=True).exists():
        messages.error(request, "That item could not be found.")
        return redirect("inventory:catalog")
    try:
        quantity = max(1, int(request.POST.get("quantity") or 1))
    except (TypeError, ValueError):
        quantity = 1

    raw = request.session.get(CART_SESSION_KEY, {})
    entries = raw.get(action, [])
    existing = next((e for e in entries if e.get("item_id") == item_id), None)
    if existing:
        existing["quantity"] = quantity
    else:
        entries.append({"item_id": item_id, "quantity": quantity})
    raw[action] = entries
    request.session[CART_SESSION_KEY] = raw

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "counts": _cart_counts(request)})

    messages.success(request, "Added to your cart.")
    return redirect("inventory:catalog")


@require_POST
@csrf_exempt
def cart_remove(request):
    if not request.user.is_authenticated:
        return redirect("inventory:cart")
    action = request.POST.get("action", "")
    try:
        item_id = int(request.POST.get("item_id"))
    except (TypeError, ValueError):
        item_id = None
    raw = request.session.get(CART_SESSION_KEY, {})
    if action in raw and item_id is not None:
        raw[action] = [e for e in raw[action] if e.get("item_id") != item_id]
        request.session[CART_SESSION_KEY] = raw
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "counts": _cart_counts(request), "message": f"Removed from your {action.replace('_', ' ')} cart."})
    messages.success(request, f"Removed from your {action.replace('_', ' ')} cart.")
    return redirect("inventory:cart")


@require_POST
@csrf_exempt
def cart_clear(request):
    if not request.user.is_authenticated:
        return redirect("inventory:cart")
    action = request.POST.get("action") or ""
    raw = request.session.get(CART_SESSION_KEY, {})
    if action:
        raw[action] = []
    else:
        raw = {}
    request.session[CART_SESSION_KEY] = raw
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "counts": _cart_counts(request), "message": "Cart cleared."})
    messages.success(request, "Cart cleared.")
    return redirect("inventory:catalog")


def home(request):
    if not request.user.is_authenticated:
        return render(request, "inventory/landing.html")

    if is_admin_or_above(request.user):
        return redirect("inventory:dashboard")

    if request.user.is_staff or request.user.is_superuser:
        return redirect("inventory:staff_home")

    if role_of(request.user) == ROLE_TEACHER:
        return redirect("inventory:teacher_home")

    item_qs = (
        StockEntry.objects.filter(is_active=True)
        .filter(location__in=visible_locations(request.user))
        .select_related("location", "status")
        .order_by("catalog_item__category", "catalog_item__subcategory", "catalog_item__name")
    )
    locations = visible_locations(request.user)
    workspace_access = user_has_workspace_access(request.user)
    conditions = ConditionOption.objects.filter(is_active=True).order_by("name")
    total_items = item_qs.count()
    total_available = sum(item.quantity_available for item in item_qs)
    total_out = sum(item.quantity_out for item in item_qs)
    items = list(item_qs)
    for item in items:
        desc = (item.catalog_item.description or "").strip()
        item.type_label = desc if not desc.startswith("Imported from") else ""
        item.group_label = (item.catalog_item.category or "").strip() or (item.type_label or "Items")
    borrowed_items = get_user_borrowed_items(request.user)
    has_active_gear = bool(borrowed_items)
    now = timezone.now()
    announcements = (
        Announcement.objects.filter(is_active=True)
        .filter(Q(visible_until__isnull=True) | Q(visible_until__gte=now))
        .order_by("-created_at")
    )
    slip_request = _pop_slip_request(request)
    active_loans = get_user_active_loans(request.user)
    return render(
        request,
        "inventory/home.html",
        {
            "items": items,
            "locations": locations,
            "conditions": conditions,
            "workspace_access": workspace_access,
            "can_book_ahead": can_book_ahead(request.user),
            "can_check_out": can_check_out(request.user),
            "can_check_in": can_check_in(request.user, has_active_gear),
            "total_items": total_items,
            "total_available": total_available,
            "total_out": total_out,
            "borrowed_items": borrowed_items,
            "announcements": announcements,
            "pending_requests": _home_pending_requests(request.user),
            "reservations": _home_reservations(request.user),
            "active_loans": active_loans,
            "slip_request": slip_request,
            "slip_return_url": _slip_return_url(request, slip_request),
        },
    )


def teacher_home(request):
    """Landing page for teachers. They can do everything a student can (browse,
    book ahead, check out, check in) and additionally send items to maintenance."""
    if not request.user.is_authenticated:
        return render(request, "inventory/landing.html")

    if role_of(request.user) != ROLE_TEACHER:
        return redirect("inventory:home")

    item_qs = (
        StockEntry.objects.filter(is_active=True)
        .filter(location__in=visible_locations(request.user))
        .select_related("location", "status")
        .order_by("catalog_item__category", "catalog_item__subcategory", "catalog_item__name")
    )
    locations = visible_locations(request.user)
    workspace_access = user_has_workspace_access(request.user)
    conditions = ConditionOption.objects.filter(is_active=True).order_by("name")
    total_items = item_qs.count()
    total_available = sum(item.quantity_available for item in item_qs)
    total_out = sum(item.quantity_out for item in item_qs)
    items = list(item_qs)
    for item in items:
        desc = (item.catalog_item.description or "").strip()
        item.type_label = desc if not desc.startswith("Imported from") else ""
        item.group_label = (item.catalog_item.category or "").strip() or (item.type_label or "Items")
    borrowed_items = get_user_borrowed_items(request.user)
    has_active_gear = bool(borrowed_items)
    now = timezone.now()
    announcements = (
        Announcement.objects.filter(is_active=True)
        .filter(Q(visible_until__isnull=True) | Q(visible_until__gte=now))
        .order_by("-created_at")
    )
    slip_request = _pop_slip_request(request)
    active_loans = get_user_active_loans(request.user)
    return render(
        request,
        "inventory/teacher_home.html",
        {
            "items": items,
            "locations": locations,
            "conditions": conditions,
            "workspace_access": workspace_access,
            "can_book_ahead": can_book_ahead(request.user),
            "can_check_out": can_check_out(request.user),
            "can_check_in": can_check_in(request.user, has_active_gear),
            "total_items": total_items,
            "total_available": total_available,
            "total_out": total_out,
            "borrowed_items": borrowed_items,
            "announcements": announcements,
            "pending_requests": _home_pending_requests(request.user),
            "reservations": _home_reservations(request.user),
            "active_loans": active_loans,
            "slip_request": slip_request,
            "slip_return_url": _slip_return_url(request, slip_request),
        },
    )


@user_passes_test(lambda user: user.is_staff)
def staff_home(request):
    """Staff desk.

    A single quick-action workspace for counter staff: pick an action, then
    type or scan a slip code, scan a QR/SKU, or choose items. The slip-code
    field (shown only for check-out / check-in) resolves a member's loan
    instantly so the desk can hand over or check gear in on their behalf.
    """
    locations = visible_locations(request.user)
    conditions = ConditionOption.objects.filter(is_active=True).order_by("name")
    workspace_access = user_has_workspace_access(request.user)
    borrowed_items = get_user_borrowed_items(request.user)
    has_active_gear = bool(borrowed_items)
    active_loans = get_user_active_loans(request.user)
    item_qs = (
        StockEntry.objects.filter(is_active=True)
        .filter(location__in=locations)
        .select_related("location", "status")
        .order_by("catalog_item__category", "catalog_item__subcategory", "catalog_item__name")
    )
    items = list(item_qs)
    for item in items:
        desc = (item.catalog_item.description or "").strip()
        item.type_label = desc if not desc.startswith("Imported from") else ""
        item.group_label = (item.catalog_item.category or "").strip() or (item.type_label or "Items")

    return render(
        request,
        "inventory/staff_home.html",
        {
            "items": items,
            "locations": locations,
            "conditions": conditions,
            "workspace_access": workspace_access,
            "can_book_ahead": can_book_ahead(request.user),
            "can_check_out": can_check_out(request.user),
            "can_check_in": can_check_in(request.user, has_active_gear),
            "active_loans": active_loans,
        },
    )


@login_required
@ensure_csrf_cookie
def catalog(request):
    q = (request.GET.get("q") or "").strip()
    item_qs = (
        StockEntry.objects.filter(is_active=True)
        .select_related("location", "status")
        .order_by("catalog_item__category", "catalog_item__subcategory", "catalog_item__name")
    )
    if not request.user.is_staff:
        item_qs = item_qs.filter(location__in=visible_locations(request.user))
    if q:
        item_qs = item_qs.filter(
            Q(name__icontains=q)
            | Q(sku__icontains=q)
            | Q(category__icontains=q)
            | Q(subcategory__icontains=q)
            | Q(description__icontains=q)
        )
    items = list(item_qs)
    for item in items:
        desc = (item.catalog_item.description or "").strip()
        item.type_label = desc if not desc.startswith("Imported from") else ""
        item.group_label = (item.catalog_item.category or "").strip() or (item.type_label or "Items")
    categories = sorted({item.group_label for item in items if item.group_label})
    locations = visible_locations(request.user).order_by("name")
    return render(
        request,
        "inventory/catalog.html",
        {
            "items": items,
            "locations": locations,
            "categories": categories,
            "can_check_out": can_check_out(request.user),
            "cart_counts": _cart_counts(request),
            "search_query": q,
        },
    )


@login_required
def catalog_stock(request):
    """Lightweight JSON endpoint returning current stock for specific items.

    Used by the catalog page to keep the on-screen quantities fresh without
    re-rendering the entire product grid."""
    ids = request.GET.get("ids", "")
    id_list = [i.strip() for i in ids.split(",") if i.strip().isdigit()]
    items = StockEntry.objects.filter(pk__in=id_list).only("pk", "quantity_total", "quantity_out", "quantity_maintenance")
    data = {}
    for item in items:
        data[str(item.pk)] = {
            "available": item.quantity_available,
            "total": item.quantity_total,
            "out": item.quantity_out,
            "maintenance": item.quantity_maintenance,
        }
    return JsonResponse({"items": data})


@login_required
def cart(request):
    """Show the cart page (GET) or submit all cart items (POST)."""
    raw = request.session.get(CART_SESSION_KEY, {})
    actions = [("book_ahead", "Book ahead"), ("check_out", "Check out")]
    cart_items = []
    for action, label in actions:
        entries = raw.get(action, [])
        if not entries:
            continue
        resolved = []
        for entry in entries:
            stock_entry = StockEntry.objects.filter(pk=entry.get("item_id"), is_active=True).select_related("catalog_item", "location").first()
            if not stock_entry:
                continue
            resolved.append(
                {
                    "item_id": stock_entry.pk,
                    "name": stock_entry.name,
                    "location": stock_entry.location.name if stock_entry.location else "",
                    "location_id": stock_entry.location.pk if stock_entry.location else "",
                    "quantity": max(1, int(entry.get("quantity") or 1)),
                    "quantity_available": stock_entry.quantity_available,
                }
            )
        if resolved:
            cart_items.append({"action": action, "label": label, "entries": resolved})

    if request.method == "POST":
        if not cart_items:
            messages.info(request, "Your cart is empty.")
            return redirect("inventory:catalog")

        # Read shared fields from the cart form.
        taken_at_raw = request.POST.get("taken_at") or None
        expected_return_raw = request.POST.get("expected_return") or None
        notes = (request.POST.get("notes") or "").strip()

        taken_at = parse_datetime(taken_at_raw) if taken_at_raw else None
        expected_return = parse_datetime(expected_return_raw) if expected_return_raw else None

        # Validate required fields.
        errors = []
        if not notes:
            errors.append("Purpose is required.")
        has_book_ahead = any(group["action"] == "book_ahead" for group in cart_items)
        has_check_out = any(group["action"] == "check_out" for group in cart_items)
        if has_book_ahead and not taken_at:
            errors.append("Expected Checkout is required for book-ahead items.")
        if has_book_ahead and not expected_return:
            errors.append("Expected return is required for book-ahead items.")
        if has_check_out and not expected_return:
            errors.append("Expected return is required for check-out items.")
        if taken_at_raw and taken_at is None:
            errors.append("Expected Checkout date is not valid.")
        if expected_return_raw and expected_return is None:
            errors.append("Expected return date is not valid.")
        if errors:
            for err in errors:
                messages.error(request, err)
            return redirect("inventory:cart")

        # Build item entries from the cart, reading the per-item action/qty.
        item_entries = []
        for group in cart_items:
            for entry in group["entries"]:
                pk = entry["item_id"]
                action_val = request.POST.get("action_" + str(pk), group["action"])
                qty_val = request.POST.get("qty_" + str(pk))
                try:
                    qty = max(1, int(qty_val)) if qty_val else entry["quantity"]
                except (TypeError, ValueError):
                    qty = entry["quantity"]
                item_entries.append({
                    "item": StockEntry.objects.get(pk=pk),
                    "quantity": qty,
                    "location_id": entry.get("location_id"),
                    "action": action_val,
                })

        # Group by action type so each request has a single transaction_type.
        by_action = {}
        for entry in item_entries:
            by_action.setdefault(entry["action"], []).append(entry)

        created_any = False
        for action_type, entries in by_action.items():
            if action_type == "check_out" and not can_check_out(request.user):
                messages.error(request, "You must sign in with a kwenamusic.co.za account to check gear out.")
                return redirect("inventory:cart")

            request_item_entries = []
            for entry in entries:
                row_location = None
                loc_id = request.POST.get("location_id_" + str(entry["item"].pk))
                if loc_id:
                    row_location = Location.objects.filter(pk=loc_id).first()
                if row_location is None:
                    row_location = entry["item"].location
                request_item_entries.append({
                    "item": entry["item"],
                    "quantity": entry["quantity"],
                    "location": row_location,
                    "condition": None,
                })

            request_obj, error = create_request(
                request.user,
                action_type,
                request_item_entries,
                notes=notes,
                expected_return=expected_return,
                taken_at=taken_at,
            )
            if error:
                messages.error(request, error)
                return redirect("inventory:cart")

            if is_superadmin(request.user):
                ok, error = apply_request(request_obj, decided_by=request.user)
                if not ok:
                    messages.error(request, error)
                    return redirect("inventory:cart")
                label = action_type.replace("_", " ").title()
                item_count = request_obj.items.count()
                messages.success(
                    request,
                    f"Auto-approved {label} for {item_count} item(s). Stock has moved.",
                )
            else:
                label = action_type.replace("_", " ").title()
                item_count = request_obj.items.count()
                messages.success(
                    request,
                    f"{label} request submitted for {item_count} item(s). An admin must approve it before any stock moves.",
                )
            created_any = True

        if created_any:
            request.session[CART_SESSION_KEY] = {}
            messages.info(request, "Your cart has been submitted.")
            return redirect("inventory:catalog")

    locations = visible_locations(request.user).order_by("name")
    conditions = ConditionOption.objects.filter(is_active=True).order_by("name")
    return render(
        request,
        "inventory/cart.html",
        {
            "cart_items": cart_items,
            "locations": locations,
            "conditions": conditions,
            "can_book_ahead": can_book_ahead(request.user),
            "can_check_out": can_check_out(request.user),
        },
    )


def guest_login(request):
    if request.method == "POST":
        form = GuestLoginForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            if email:
                # An email is a real identifier: reuse the same guest account
                # for repeat visits so their history stays together.
                username = f"guest-{email.lower()}"
                user, created = User.objects.get_or_create(username=username)
            else:
                # No email was given, so there is nothing reliable to key an
                # existing account on. Reusing a slug built from the name
                # would let two different people who share a name (not
                # unlikely at a school) land in the same account and see
                # each other's borrowed gear and notifications, so give every
                # no-email guest submission its own fresh account instead.
                username = f"guest-{uuid.uuid4().hex[:12]}"
                user = User.objects.create(username=username)
                created = True
            if created:
                user.set_unusable_password()
                user.save()
            profile, _ = GuestProfile.objects.get_or_create(user=user, defaults={
                "full_name": form.cleaned_data["name"],
                "email": form.cleaned_data["email"],
                "phone": form.cleaned_data["phone"],
                "organization": form.cleaned_data["organization"],
            })
            profile.full_name = form.cleaned_data["name"]
            profile.email = form.cleaned_data["email"]
            profile.phone = form.cleaned_data["phone"]
            profile.organization = form.cleaned_data["organization"]
            profile.save()
            user.backend = "django.contrib.auth.backends.ModelBackend"
            auth_login(request, user)
            messages.success(request, "Guest session created successfully.")
            return redirect("inventory:home")
    else:
        form = GuestLoginForm()
    return render(request, "inventory/guest_login.html", {"form": form})


def local_login(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            next_url = request.POST.get("next") or request.GET.get("next") or ""
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect("inventory:home")
    else:
        form = AuthenticationForm()
    return render(
        request,
        "inventory/login.html",
        {"form": form, "next": request.GET.get("next", "")},
    )


def local_logout(request):
    auth_logout(request)
    messages.info(request, "You have signed out.")
    return redirect("inventory:landing")


@require_POST
@login_required
def mark_onboarding_seen(request):
    """Dismiss the first-login onboarding popup.

    ``permanent=1`` writes ``has_seen_onboarding`` to the profile so the modal
    never returns on future logins; otherwise only the session flag is cleared
    and the popup resurfaces on the next login (a "skip for now" behaviour)."""
    permanent = request.POST.get("permanent") == "1"
    profile = ensure_profile(request.user)
    if permanent:
        profile.has_seen_onboarding = True
        profile.save(update_fields=["has_seen_onboarding"])
    request.session.pop("show_onboarding", None)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "permanent": permanent})
    return redirect("inventory:home")


@login_required
def account_page(request):
    user = request.user
    guest_profile = getattr(user, "guest_profile", None)
    is_guest = guest_profile is not None
    now = timezone.now()

    borrowed_items = get_user_borrowed_items(user)
    gear_summary = get_user_gear_summary(user)
    announcements = (
        Announcement.objects.filter(is_active=True)
        .filter(Q(visible_until__isnull=True) | Q(visible_until__gte=now))
        .order_by("-created_at")
    )
    user_notifications_qs = (
        Notification.objects.filter(user=user).select_related("transaction").order_by("-created_at")
    )
    user_notifications_unread = user_notifications_qs.filter(is_read=False).count()
    paginator = Paginator(user_notifications_qs, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)
    overdue_items = [entry for entry in borrowed_items if entry["is_overdue"]]
    request_history = get_user_request_history(user)

    return render(
        request,
        "inventory/account.html",
        {
            "is_guest": is_guest,
            "guest_profile": guest_profile,
            "borrowed_items": borrowed_items,
            "reservations": gear_summary["reservations"],
            "pending_requests": gear_summary["pending"],
            "request_history": request_history,
            "notifications": announcements,
            "user_notifications": page_obj,
            "user_notifications_unread": user_notifications_unread,
            "overdue_items": overdue_items,
        },
    )


@login_required
@require_POST
def notifications_mark_read(request):
    """Mark the signed-in user's personal notifications as read.

    An optional ``pk`` marks a single notification; otherwise every unread one
    is cleared.
    """
    qs = Notification.objects.filter(user=request.user, is_read=False)
    pk = request.POST.get("pk")
    if pk:
        qs = qs.filter(pk=pk)
    qs.update(is_read=True)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect("inventory:account")


@login_required
@require_POST
def notifications_delete_all(request):
    """Delete every one of the signed-in user's personal notifications."""
    result = Notification.objects.filter(user=request.user).delete()
    # QuerySet.delete() returns (total_deleted, {model: count}). The response
    # only needs the total.
    deleted = result[0] if isinstance(result, tuple) else sum(result.values())
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "deleted": deleted})
    messages.success(request, "All notifications cleared.")
    return redirect("inventory:account")


@login_required
def account_edit(request):
    user = request.user
    guest_profile = getattr(user, "guest_profile", None)
    is_guest = guest_profile is not None

    if is_guest:
        form = GuestProfileForm(request.POST or None, instance=guest_profile)
    else:
        form = LocalAccountForm(request.POST or None, instance=user)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Your details have been updated.")
        return redirect("inventory:account")

    return render(
        request,
        "inventory/account_edit.html",
        {"form": form, "is_guest": is_guest},
    )


@require_POST
def process_transaction(request):
    if not request.user.is_authenticated:
        return redirect("inventory:guest_login")

    transaction_type = request.POST.get("transaction_type", "")
    if transaction_type == "check_out" and not can_check_out(request.user):
        messages.error(request, "You must sign in with a kwenamusic.co.za account to check gear out.")
        return redirect("inventory:home")
    if transaction_type == "check_in" and not can_check_in(request.user):
        messages.error(request, "You can only check in gear you currently have out.")
        return redirect("inventory:home")
    # book_ahead is allowed for any signed-in user.

    # An admin scanning a book-ahead's slip on the quick-action check-out form
    # hands the reservation over at pickup: the reserved stock becomes a live
    # check-out, no new request is created. Only staff may do this.
    hand_over_id = request.POST.get("hand_over_request_id", "").strip()
    if hand_over_id and is_superadmin(request.user):
        from .models import Request

        try:
            hand_over_obj = Request.objects.filter(
                pk=int(hand_over_id), source_request__isnull=True
            ).first()
        except (ValueError, TypeError):
            hand_over_obj = None
        if hand_over_obj is None:
            messages.error(request, "That reservation could not be found.")
            return redirect("inventory:staff_home")
        ok, error = hand_over_request(hand_over_obj, decided_by=request.user)
        if not ok:
            messages.error(request, error)
            return redirect("inventory:staff_home")
        count = hand_over_obj.items.count()
        messages.success(
            request,
            f"Handed over {count} item(s) to {hand_over_obj.person_name or hand_over_obj.user.username}. Stock is now out.",
        )
        request.session["slip_request_id"] = hand_over_obj.pk
        return redirect("inventory:staff_home")

    form = TransactionForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Please correct the form and try again.")
        return redirect("inventory:home")

    transaction_type = form.cleaned_data["transaction_type"]
    asset_tag = form.cleaned_data.get("asset_tag", "") or ""
    notes = form.cleaned_data["notes"]
    expected_return = form.cleaned_data.get("expected_return")
    taken_at = form.cleaned_data.get("taken_at")

    item_entries = []
    if asset_tag.strip():
        try:
            item = StockEntry.objects.get(sku__iexact=asset_tag.strip())
        except StockEntry.DoesNotExist:
            messages.error(request, "Asset tag not found. Please check the SKU and try again.")
            return redirect("inventory:home")
        quantities = request.POST.getlist("quantities")
        qty_val = 1
        if quantities and quantities[0].strip():
            try:
                qty_val = max(1, int(quantities[0]))
            except ValueError:
                qty_val = 1
        item_entries.append({"item": item, "quantity": qty_val})
    else:
        item_ids = request.POST.getlist("item_ids")
        quantities = request.POST.getlist("quantities")
        for index, item_id in enumerate(item_ids):
            if not item_id.strip():
                continue
            try:
                item = StockEntry.objects.get(pk=int(item_id))
            except (ValueError, StockEntry.DoesNotExist):
                messages.error(request, "Please choose valid items for the submission.")
                return redirect("inventory:home")
            try:
                quantity_value = int(quantities[index]) if index < len(quantities) and quantities[index].strip() else 1
            except (ValueError, TypeError):
                quantity_value = 1
            item_entries.append({"item": item, "quantity": max(1, quantity_value)})

        if not item_entries:
            messages.error(request, "Please select at least one item or use the scan option.")
            return redirect("inventory:home")

    location_ids = request.POST.getlist("location_ids")
    condition_ids = request.POST.getlist("condition_ids")

    # Attach the per-row location/condition so the whole submission becomes one
    # Request with one line per item. A single admin decision covers everything.
    for index, entry in enumerate(item_entries):
        item = entry["item"]
        raw_loc = location_ids[index] if index < len(location_ids) else ""
        raw_cond = condition_ids[index] if index < len(condition_ids) else ""
        row_location = Location.objects.filter(pk=raw_loc).first() if raw_loc else None
        # Book-ahead reservations and check-outs assume working gear, so
        # condition is not collected for them.
        row_condition = None
        if transaction_type not in ("book_ahead", "check_out"):
            row_condition = ConditionOption.objects.filter(pk=raw_cond).first() if raw_cond else None
        if row_location is None:
            row_location = item.location
        entry["location"] = row_location
        entry["condition"] = row_condition

    # A check-in closes an existing loan, so file it against that loan's request
    # and reuse its slip code instead of minting a new one. Prefer the loan the
    # member explicitly picks (by slip code); otherwise auto-detect only when
    # unambiguous, else leave it null so the form can ask.
    source_request = None
    submitted_source = request.POST.get("source_request_id", "").strip()
    checked_in_by_admin = bool(request.POST.get("checked_in_by_admin")) and is_superadmin(request.user)
    if transaction_type == "check_in" and item_entries:
        source_request = _resolve_checkin_source(
            request.user, item_entries[0]["item"], submitted_source,
            as_staff=checked_in_by_admin,
        )
        if submitted_source and source_request is None:
            messages.error(
                request,
                "That slip code isn't one of your active loans for this item. "
                "Choose the correct loan from the list or ask at the counter.",
            )
            return redirect("inventory:staff_home" if checked_in_by_admin else ("inventory:teacher_home" if role_of(request.user) == ROLE_TEACHER else "inventory:home"))

    # When an admin checks gear in at the counter on a member's behalf, file a
    # return request against the loan (so it shows in the slip history/print)
    # and approve it immediately so the stock moves right away. The note records
    # that staff handled the return.
    if checked_in_by_admin and source_request is not None:
        admin_tag = "Checked in by Superadmin" + ((": " + display_name(request.user)) if display_name(request.user) else "")
        return_obj, error = create_request(
            request.user,
            "check_in",
            item_entries,
            notes=admin_tag,
            expected_return=expected_return,
            taken_at=taken_at,
            source_request=source_request,
        )
        if error:
            messages.error(request, error)
            return redirect("inventory:staff_home")
        ok, error = apply_request(return_obj, decided_by=request.user)
        if not ok:
            messages.error(request, error)
            return redirect("inventory:staff_home")
        item_count = return_obj.items.count()
        messages.success(
            request,
            f"Checked in {item_count} item(s) on behalf of {source_request.person_name or source_request.user.username}. Stock is back.",
        )
        request.session["slip_request_id"] = source_request.pk
        return redirect("inventory:staff_home")

    request_obj, error = create_request(
        request.user,
        transaction_type,
        item_entries,
        notes=notes,
        expected_return=expected_return,
        taken_at=taken_at,
        source_request=source_request,
    )
    if error:
        messages.error(request, error)
        return redirect("inventory:home")

    notify_request_received(request_obj)

    if is_superadmin(request.user):
        ok, error = apply_request(request_obj, decided_by=request.user)
        if not ok:
            messages.error(request, error)
            return redirect("inventory:staff_home" if request.user.is_staff else "inventory:home")
        label = transaction_type.replace("_", " ").title()
        item_count = request_obj.items.count()
        messages.success(
            request,
            f"Auto-approved {label} for {item_count} item(s). Stock has moved.",
        )
    else:
        label = transaction_type.replace("_", " ").title()
        item_count = request_obj.items.count()
        messages.success(
            request,
            f"{label} request submitted for {item_count} item(s). An admin must approve it before any stock moves.",
        )
    # Remove the submitted items from the session cart so it does not linger
    # after a successful request.
    submitted_ids = {entry["item"].pk for entry in item_entries}
    raw = request.session.get(CART_SESSION_KEY)
    if raw:
        for key in ("book_ahead", "check_out"):
            if key in raw:
                raw[key] = [e for e in raw[key] if e.get("item_id") not in submitted_ids]
        request.session[CART_SESSION_KEY] = raw
    # Send the member back to their home page and surface the slip as a
    # one-shot popup there instead of navigating to a standalone receipt page.
    request.session["slip_request_id"] = request_obj.pk
    if is_admin_or_above(request.user):
        return redirect("inventory:staff_home")
    return redirect("inventory:teacher_home" if role_of(request.user) == ROLE_TEACHER else "inventory:home")


@require_POST
def scan_item(request):
    if not request.user.is_authenticated:
        return redirect("inventory:guest_login")

    transaction_type = form.cleaned_data.get("transaction_type", "") if request.method == "POST" else ""
    if transaction_type == "check_out" and not can_check_out(request.user):
        messages.error(request, "You must sign in with a kwenamusic.co.za account to check gear out.")
        return redirect("inventory:home")
    if transaction_type == "check_in" and not can_check_in(request.user):
        messages.error(request, "You can only check in gear you currently have out.")
        return redirect("inventory:home")

    form = ScanItemForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Please correct the scan form and try again.")
        return redirect("inventory:home")

    asset_tag = form.cleaned_data["asset_tag"].strip()
    transaction_type = form.cleaned_data["transaction_type"]
    condition = form.cleaned_data.get("condition")
    notes = form.cleaned_data["notes"]
    location = form.cleaned_data.get("location")
    expected_return = form.cleaned_data.get("expected_return")

    try:
        item = StockEntry.objects.get(catalog_item__sku__iexact=asset_tag)
    except StockEntry.DoesNotExist:
        messages.error(request, "Asset tag not found. Please check the SKU and try again.")
        return redirect("inventory:home")

    # A check-in closes an existing loan, so file it against that loan's request
    # and reuse its slip code instead of minting a new one. Prefer the loan the
    # member explicitly picks (by slip code); otherwise auto-detect only when
    # unambiguous, else leave it null so the form can ask.
    source_request = None
    submitted_source = request.POST.get("source_request_id", "").strip()
    if transaction_type == "check_in":
        source_request = _resolve_checkin_source(request.user, item, submitted_source)
        if submitted_source and source_request is None:
            messages.error(
                request,
                "That slip code isn't one of your active loans for this item. "
                "Choose the correct loan from the list or ask at the counter.",
            )
            return redirect("inventory:teacher_home" if role_of(request.user) == ROLE_TEACHER else "inventory:home")

    request_obj, error = create_request(
        request.user,
        transaction_type,
        [{"item": item, "quantity": 1, "location": location or item.location, "condition": condition}],
        notes=notes,
        expected_return=expected_return,
        source_request=source_request,
    )
    if error:
        messages.error(request, error)
        return redirect("inventory:home")

    notify_request_received(request_obj)

    if is_superadmin(request.user):
        ok, error = apply_request(request_obj, decided_by=request.user)
        if not ok:
            messages.error(request, error)
            return redirect("inventory:staff_home" if request.user.is_staff else "inventory:home")
        label = transaction_type.replace("_", " ").title()
        messages.success(request, f"Auto-approved {label} for {item.name}. Stock has moved.")
    else:
        label = transaction_type.replace("_", " ").title()
        messages.success(request, f"{label} request submitted for {item.name}. An admin must approve it before any stock moves.")
    # Send the member back to their home page and surface the slip as a
    # one-shot popup there instead of navigating to a standalone receipt page.
    request.session["slip_request_id"] = request_obj.pk
    if is_admin_or_above(request.user):
        return redirect("inventory:staff_home")
    return redirect("inventory:teacher_home" if role_of(request.user) == ROLE_TEACHER else "inventory:home")


@user_passes_test(is_superadmin)
def return_by_code(request):
    """Desk return flow keyed by a request's slip code (e.g. KW-0042).

    Staff enters the code, then scans/selects the items coming back. Each return
    is linked to the exact loan spawned by that request, so the request status
    updates precisely instead of relying on loan inference."""
    from django.db.models import Q

    reference_code = request.GET.get("code", "").strip()
    request_obj = None
    if request.method == "POST":
        reference_code = request.POST.get("reference_code", "").strip()
        # Resolve to the loan (a check-in reuses its loan's code), exactly as
        # return_request_by_code does, so the request shown on screen is the
        # one the return is actually filed against.
        def _lookup(code):
            return Request.objects.filter(
                reference_code__iexact=code, source_request__isnull=True
            ).select_related("user").prefetch_related(
                "items__item", "items__transaction"
            ).first()

        if "lookup" in request.POST:
            request_obj = _lookup(reference_code)
            if not request_obj:
                messages.error(request, "No request found with that code.")
        elif "return_items" in request.POST:
            request_obj = _lookup(reference_code)
            item_ids = request.POST.getlist("item_ids")
            quantities = request.POST.getlist("quantities")
            item_entries = []
            missing = []
            for index, item_id in enumerate(item_ids):
                if not item_id.strip():
                    continue
                try:
                    item = StockEntry.objects.get(pk=int(item_id))
                except (ValueError, StockEntry.DoesNotExist):
                    missing.append(item_id)
                    continue
                qty = 1
                if index < len(quantities) and quantities[index].strip():
                    try:
                        qty = max(1, int(quantities[index]))
                    except ValueError:
                        qty = 1
                item_entries.append({"item": item, "quantity": qty})
            if missing:
                messages.error(request, f"Could not find item(s): {', '.join(missing)}.")
            elif not request_obj:
                messages.error(request, "Look the request up first.")
            else:
                ok, msg = return_request_by_code(
                    reference_code, item_entries, decided_by=request.user
                )
                if ok:
                    messages.success(request, msg)
                    return redirect("inventory:return_by_code")
                messages.error(request, msg)
                # Keep the looked-up request visible after an error.
                request_obj = Request.objects.filter(
                    reference_code__iexact=reference_code, source_request__isnull=True
                ).select_related("user").prefetch_related("items__item", "items__transaction").first()

    return render(
        request,
        "inventory/return_by_code.html",
        {"reference_code": reference_code, "request_obj": request_obj},
    )


@user_passes_test(lambda user: user.is_staff)
def announcements_page(request):
    announcements = Announcement.objects.all().order_by("-created_at")
    return render(request, "inventory/announcements.html", {"announcements": announcements})


@user_passes_test(lambda user: user.is_staff)
def post_announcement(request):
    if request.method == "POST":
        form = AnnouncementForm(request.POST)
        if form.is_valid():
            announcement = form.save(commit=False)
            announcement.created_by = request.user
            announcement.created_by_name = display_name(request.user)
            announcement.is_active = True
            announcement.save()
            messages.success(request, "Update posted for students.")
            return redirect("inventory:announcements")
        messages.error(request, "Please complete the message and try again.")
    else:
        form = AnnouncementForm()
    return render(
        request,
        "inventory/announcement_form.html",
        {"form": form, "is_edit": False},
    )


@user_passes_test(lambda user: user.is_staff)
def announcement_edit(request, pk):
    announcement = get_object_or_404(Announcement, pk=pk)
    if request.method == "POST":
        form = AnnouncementForm(request.POST, instance=announcement)
        if form.is_valid():
            form.save()
            messages.success(request, "Update saved.")
            return redirect("inventory:announcements")
        messages.error(request, "Please complete the message and try again.")
    else:
        form = AnnouncementForm(instance=announcement)
    return render(
        request,
        "inventory/announcement_form.html",
        {"form": form, "announcement": announcement, "is_edit": True},
    )


@user_passes_test(lambda user: user.is_staff)
def announcement_toggle(request, pk):
    if request.method == "POST":
        announcement = Announcement.objects.filter(pk=pk).first()
        if announcement:
            announcement.is_active = not announcement.is_active
            announcement.save()
    return redirect("inventory:announcements")


@user_passes_test(lambda user: user.is_staff)
def announcement_pin(request, pk):
    if request.method == "POST":
        announcement = get_object_or_404(Announcement, pk=pk)
        announcement.pinned = not announcement.pinned
        announcement.save(update_fields=["pinned"])
        if announcement.pinned:
            messages.success(request, "Update pinned to the top.")
        else:
            messages.success(request, "Update unpinned.")
    return redirect("inventory:announcements")


@user_passes_test(lambda user: user.is_staff)
def announcement_delete(request, pk):
    if request.method == "POST":
        Announcement.objects.filter(pk=pk).delete()
        messages.success(request, "Update removed.")
    return redirect("inventory:announcements")


@user_passes_test(lambda user: user.is_staff)
def dashboard(request):
    items = StockEntry.objects.select_related("catalog_item", "location").all().order_by("location__name", "catalog_item__name")
    total_items = items.count()
    total_available = sum(item.quantity_available for item in items)
    total_out = sum(item.quantity_out for item in items)
    locations = Location.objects.filter(is_active=True)
    conditions = ConditionOption.objects.filter(is_active=True).order_by("name")
    now = timezone.now()
    pending_returns = []

    live_txns = []
    for item in StockEntry.objects.filter(quantity_out__gt=0).select_related("catalog_item", "location"):
        live_out = item.quantity_out
        for txn in item.transactions.filter(
            transaction_type="check_out", approval_status="approved", voided="none"
        ).select_related("user").order_by("-created_at"):
            if live_out <= 0:
                break
            live_out -= txn.quantity
            live_txns.append((item, txn))

    request_items = RequestItem.objects.filter(
        transaction__in=[txn for _, txn in live_txns]
    ).select_related("request", "transaction")
    txn_to_request = {ri.transaction_id: ri.request for ri in request_items}

    request_groups = {}
    unlinked = []
    for item, txn in live_txns:
        if txn.expected_return:
            req = txn_to_request.get(txn.pk)
            if req:
                group = request_groups.get(req.pk)
                if group is None:
                    group = {
                        "request": req,
                        "items": set(),
                        "overdue": False,
                        "expected_return": None,
                    }
                    request_groups[req.pk] = group
                group["items"].add(item.name)
                if txn.expected_return < now:
                    group["overdue"] = True
                if group["expected_return"] is None or txn.expected_return < group["expected_return"]:
                    group["expected_return"] = txn.expected_return
            else:
                unlinked.append((item, txn))

    for group in request_groups.values():
        pending_returns.append(
            {
                "request": group["request"],
                "member": group["request"].user.get_full_name() or group["request"].user.username,
                "items": ", ".join(sorted(group["items"])),
                "expected_return": group["expected_return"],
                "overdue": group["overdue"],
            }
        )

    for item, txn in unlinked:
        pending_returns.append(
            {
                "item": item,
                "member": txn.user.get_full_name() or txn.user.username if txn.user else "",
                "expected_return": txn.expected_return,
                "overdue": txn.expected_return < now,
            }
        )

    pending_returns.sort(key=lambda entry: entry["expected_return"])
    announcements = Announcement.objects.all().order_by("-created_at")
    maintenance_count = Maintenance.objects.filter(completed_at__isnull=True).count()
    pending_count = len(pending_returns)
    pending_request_count = Transaction.objects.filter(approval_status="pending", voided="none").count()
    overdue_count = sum(1 for entry in pending_returns if entry["overdue"])
    location_count = locations.count()
    announcement_count = announcements.count()
    workspace_access = user_has_workspace_access(request.user)
    borrowed_items = get_user_borrowed_items(request.user)
    has_active_gear = bool(borrowed_items)
    active_loans = get_user_active_loans(request.user)
    recent_transactions = list(
        Transaction.objects.filter(voided="none")
        .select_related("item", "user", "location")
        .order_by("-created_at")[:15]
    )
    return render(
        request,
        "inventory/dashboard.html",
        {
            "items": items,
            "total_items": total_items,
            "total_available": total_available,
            "total_out": total_out,
            "locations": locations,
            "conditions": conditions,
            "pending_returns": pending_returns,
            "announcements": announcements,
            "maintenance_count": maintenance_count,
            "pending_count": pending_count,
            "pending_request_count": pending_request_count,
            "overdue_count": overdue_count,
            "location_count": location_count,
            "announcement_count": announcement_count,
            "workspace_access": workspace_access,
            "can_book_ahead": can_book_ahead(request.user),
            "can_check_out": can_check_out(request.user),
            "can_check_in": can_check_in(request.user, has_active_gear),
            "active_loans": active_loans,
            "last_updated": timezone.now(),
            "recent_transactions": recent_transactions,
        },
    )


@user_passes_test(lambda user: user.is_staff)
def requests_page(request):
    """Admin view: pending requests awaiting approval plus the full request
    history (all types). Approving a book-ahead does not move stock; the admin
    still scans the pickup at the counter.

    Everything is a :class:`Request` (one decision covers all of its item
    lines), so the lists are built from the Request model rather than the
    underlying Transactions.

    A ``q`` query param filters the full list by member name, slip code, item
    name or status."""
    from django.db.models import Q

    pending_requests = (
        Request.objects.filter(approval_status="pending", voided="none")
        .select_related("user", "decided_by")
        .prefetch_related("items__item", "items__location", "items__condition")
        .order_by("created_at")
    )

    search = (request.GET.get("q") or "").strip()
    all_qs = (
        Request.objects.all()
        .select_related("user", "decided_by")
        .prefetch_related(
            "items__item", "items__location",
            "returns", "returns__items__item",
        )
    )
    if search:
        all_qs = all_qs.filter(
            Q(person_name__icontains=search)
            | Q(reference_code__icontains=search)
            | Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(items__item__name__icontains=search)
            | Q(approval_status__icontains=search)
            | Q(transaction_type__icontains=search)
        ).distinct()
    all_qs = all_qs.order_by("-created_at")

    # Group every request under its desk slip code. A loan (source_request is
    # null) is the representative row for the slip; any check-in return that
    # reuses the same code is attached underneath it on the detail page. Voided
    # slips are kept in the list (greyed) so the audit trail stays visible.
    by_code = {}
    code_order = []
    for req in all_qs:
        code = req.reference_code or f"pk-{req.pk}"
        if code not in by_code:
            by_code[code] = []
            code_order.append(code)
        by_code[code].append(req)

    slips = []
    for code in code_order:
        reqs = by_code[code]
        loan = next((r for r in reqs if r.source_request_id is None), None)
        rep = loan or reqs[0]
        rep.slip_code = code
        rep.slip_returns = [r for r in reqs if r.source_request_id is not None]
        rep.slip_label, rep.slip_pill = _slip_status(rep)
        slips.append(rep)

    pending_count = pending_requests.count()
    return render(
        request,
        "inventory/requests.html",
        {
            "pending_requests": pending_requests,
            "slips": slips,
            "search": search,
            "pending_count": pending_count,
        },
    )


@login_required
def request_receipt(request, pk):
    """Slip page for a request, shown right after submission and reachable from
    a member's request history.

    Displays the request's desk slip code (KW-####) — the shared reference the
    member carries and shows at the counter, and that admins look up in every
    manage tab. Offers on-demand Print / Email of a receipt-style slip. The
    owner of the request or any staff member may view it."""
    from .models import Request

    req = get_object_or_404(
        Request.objects.select_related(
            "user", "user__user_profile", "user__guest_profile", "decided_by"
        ).prefetch_related(
            "items__item", "items__location", "items__condition",
            "returns", "returns__items__item", "returns__items__condition",
        ),
        pk=pk,
    )
    if not request.user.is_staff and req.user_id != request.user.id:
        messages.error(request, "You do not have access to that request.")
        return redirect("inventory:home")
    # Map each item to the condition it was returned in, so the print slip can
    # show the return condition on the Items table (loan lines have none).
    return_conditions = {}
    for ret in req.returns.all():
        for rline in ret.items.all():
            if rline.condition_id:
                return_conditions[rline.item_id] = rline.condition.name
    return_url = (
        request.build_absolute_uri(
            f"{reverse('inventory:return_by_code')}?code={req.reference_code}"
        )
        if req.reference_code
        else ""
    )
    return render(
        request,
        "inventory/request_receipt.html",
        {"req": req, "return_conditions": return_conditions, "return_url": return_url},
    )


@require_POST
@login_required
def request_email_slip(request, pk):
    """Email the member their desk slip on demand (from the slip popup).

    Nothing is emailed automatically on submission; the member chooses to email
    or print the slip from the popup. The owner of the request or any staff
    member may trigger this."""
    from .models import Request, notify_request_received, member_email

    req = get_object_or_404(
        Request.objects.select_related(
            "user", "user__user_profile", "user__guest_profile"
        ).prefetch_related("items__item"),
        pk=pk,
    )
    if not request.user.is_staff and req.user_id != request.user.id:
        messages.error(request, "You do not have access to that request.")
        return redirect("inventory:home")

    recipient = member_email(req.user)
    if not recipient:
        messages.error(
            request,
            "No email address on file, so the slip could not be sent. "
            f"Your slip code is {req.reference_code}.",
        )
    else:
        notification = notify_request_received(req)
        if notification is not None and notification.email_sent:
            messages.success(request, f"Slip {req.reference_code} emailed to {recipient}.")
        else:
            messages.error(
                request,
                "Could not send the slip email right now. "
                f"Your slip code is {req.reference_code}.",
            )
    return redirect("inventory:teacher_home" if role_of(request.user) == ROLE_TEACHER else "inventory:home")


def _resolve_request(pk):
    """Find the object to act on for a given pk. Returns ``(obj, is_request)``.

    New submissions are :class:`Request` objects (one decision covers every
    item line). Legacy single-item :class:`Transaction` rows (created before
    the multi-item model, or by tests) are still supported so old deep links and
    the approval endpoints keep working.
    """
    from .models import Request

    req = Request.objects.filter(pk=pk).first()
    if req is not None:
        return req, True
    txn = Transaction.objects.filter(pk=pk).first()
    return txn, False


def _slip_status(loan):
    """Return ``(label, pill_class)`` summarising the current state of a slip
    (its loan request). Used by the grouped "All requests" table and the slip
    detail page."""
    if loan.is_voided:
        return "Voided", "pill-danger"
    if loan.approval_status == "pending":
        return "Pending", "pill-danger"
    if loan.approval_status == "rejected":
        return "Rejected", "pill-danger"
    # Approved beyond this point.
    if loan.transaction_type == "check_in":
        return ("Returned", "pill-success") if loan.is_returned else ("Approved", "pill-success")
    if loan.transaction_type == "book_ahead" and not loan.is_handed_over:
        return "Reserved", "pill-gold"
    if loan.is_returned:
        return "Returned", "pill-success"
    if loan.is_handed_over:
        return "Checked Out", "pill-danger"
    return "Approved", "pill-success"


def _redirect_after_request_action(obj):
    """Send the user back to the slip detail when the request has a desk code,
    otherwise to the requests list."""
    if obj and getattr(obj, "reference_code", None):
        return redirect("inventory:request_slip", code=obj.reference_code)
    return redirect("inventory:requests")


@require_POST
@user_passes_test(is_superadmin)
def request_approve(request, pk):
    obj, is_request = _resolve_request(pk)
    if obj is None or obj.approval_status != "pending":
        messages.error(request, "That request could not be found.")
        return _redirect_after_request_action(obj)
    ok, error = apply_request(obj, decided_by=request.user)
    if not ok:
        messages.error(request, error)
    else:
        label = obj.transaction_type.replace("_", " ").title()
        count = obj.items.count() if is_request else 1
        messages.success(request, f"Approved {label} for {count} item(s).")
    return _redirect_after_request_action(obj)


@require_POST
@user_passes_test(is_superadmin)
def request_reject(request, pk):
    obj, is_request = _resolve_request(pk)
    if obj is None or obj.approval_status != "pending":
        messages.error(request, "That request could not be found.")
        return _redirect_after_request_action(obj)
    ok, error = reject_request(obj, decided_by=request.user)
    if not ok:
        messages.error(request, error)
    else:
        label = obj.transaction_type.replace("_", " ").title()
        count = obj.items.count() if is_request else 1
        messages.success(request, f"Rejected {label} request for {count} item(s).")
    return _redirect_after_request_action(obj)


@require_POST
@user_passes_test(is_superadmin)
def request_hand_over(request, pk):
    """Hand an approved book-ahead reservation over to the member at pickup.

    A book-ahead is approved as a reservation (no stock moves). This action is
    the actual pick-up: it converts the reservation into a live check-out, moving
    the stock out and recording who handed it over."""
    obj, is_request = _resolve_request(pk)
    if obj is None or not is_request:
        messages.error(request, "That request could not be found.")
        return _redirect_after_request_action(obj)
    ok, error = hand_over_request(obj, decided_by=request.user)
    if not ok:
        messages.error(request, error)
    else:
        count = obj.items.count()
        messages.success(
            request, f"Handed over {count} item(s) to {obj.person_name or obj.user.username}. Stock is now out."
        )
    return _redirect_after_request_action(obj)


@user_passes_test(lambda user: user.is_staff)
def request_slip(request, code):
    """Grouped detail view for a desk slip code (KW-####). Shows everything that
    belongs to the slip: the loan (book-ahead / check-out) and any check-in return
    that reuses the same code. Staff can approve/reject, hand over, edit or void
    the whole slip from here."""
    from .models import Request

    def _base_qs():
        return (
            Request.objects.select_related(
                "user", "decided_by", "handed_over_by", "returned_by"
            ).prefetch_related(
                "items__item", "items__location", "items__condition",
                "returns", "returns__items__item", "returns__items__condition",
                "returns__decided_by", "returns__user",
            )
        )

    loan = (
        _base_qs()
        .filter(reference_code__iexact=code, source_request__isnull=True)
        .first()
    )
    if loan is None:
        # Fall back to any request sharing the code (e.g. a return-only slip),
        # or a legacy request addressed by its pk-based pseudo code.
        loan = _base_qs().filter(reference_code__iexact=code).order_by("created_at").first()
        if loan is None and code.startswith("pk-"):
            try:
                loan = _base_qs().filter(pk=int(code[3:])).first()
            except ValueError:
                loan = None
    if loan is None:
        messages.error(request, "No request found with that slip code.")
        return redirect("inventory:requests")

    returns = list(loan.returns.all())
    # Desk returns: check-in transactions filed straight against this loan's
    # check-outs via the return-by-code path. They have no child return Request,
    # so they are voided individually by voiding the check-in transaction itself.
    from .models import RequestItem, Transaction

    checkout_ids = [line.transaction_id for line in loan.items.all() if line.transaction_id]
    desk_returns = []
    if checkout_ids:
        child_return_txn_ids = set(
            RequestItem.objects.filter(request__source_request=loan)
            .exclude(transaction__isnull=True)
            .values_list("transaction_id", flat=True)
        )
        desk_returns = list(
            Transaction.objects.filter(
                transaction_type="check_in",
                source_transaction_id__in=checkout_ids,
            )
            .exclude(id__in=child_return_txn_ids)
            .select_related("item", "condition", "user", "voided_by", "decided_by")
            .order_by("created_at")
        )
    label, pill = _slip_status(loan)
    # Merge every post-collection history event (child returns, desk returns and
    # the loan-void) into one list sorted oldest-first, so the slip timeline is
    # strictly chronological instead of grouped by kind.
    slip_events = []
    for ret in returns:
        slip_events.append(
            {"type": "child_return", "when": ret.returned_at or ret.created_at, "ret": ret}
        )
    for dret in desk_returns:
        slip_events.append(
            {"type": "desk_return", "when": dret.created_at, "dret": dret}
        )
    if loan.is_voided:
        slip_events.append(
            {"type": "loan_voided", "when": loan.voided_at or loan.created_at}
        )
    if loan.returned_at and not returns and not desk_returns:
        # Legacy return recorded straight on the loan with nothing to void.
        slip_events.append({"type": "legacy_return", "when": loan.returned_at})
    slip_events.sort(key=lambda e: e["when"] or loan.created_at)
    # Map each item to the condition it was returned in, so the print slip
    # can show the return condition on the Items table (loan lines have none).
    return_conditions = {}
    for ret in loan.returns.all():
        for rline in ret.items.all():
            if rline.condition_id:
                return_conditions[rline.item_id] = rline.condition.name
    return render(
        request,
        "inventory/request_slip.html",
        {
            "slip": loan,
            "returns": returns,
            "desk_returns": desk_returns,
            "slip_events": slip_events,
            "code": code,
            "slip_label": label,
            "slip_pill": pill,
            "return_conditions": return_conditions,
        },
    )


@require_POST
@user_passes_test(is_superadmin)
def request_void(request, pk):
    """Void a single entry chosen from a slip's history: either the loan itself
    (reversing only the loan's own stock) or one check-in return (reversing only
    that return). Other entries sharing the same slip code are left untouched, so
    voiding is specific to the history row the admin picked."""
    from .models import Request

    obj = get_object_or_404(
        Request.objects.select_related("user", "source_request"),
        pk=pk,
    )
    if obj.is_voided:
        messages.error(request, "This entry has already been voided.")
        return _redirect_to_slip(obj)

    reason = (request.POST.get("reason") or "").strip()
    ok, error = void_request(obj, request.user, reason=reason)
    if not ok:
        messages.error(request, error)
        return _redirect_to_slip(obj)

    loan = obj.source_request
    if loan is not None:
        # Voiding a check-in return puts its units back out, so the loan it
        # closed is no longer fully returned — reopen the loan's return marker.
        if loan.returned_at is not None:
            loan.returned_at = None
            loan.returned_by = None
            loan.returned_by_name = ""
            loan.save(update_fields=["returned_at", "returned_by", "returned_by_name"])
        messages.success(request, f"Voided the return on slip {obj.reference_code or obj.pk}.")
    else:
        messages.success(request, f"Voided the loan on slip {obj.reference_code or obj.pk}.")
    return _redirect_to_slip(obj)


def _redirect_to_slip(obj):
    """Send the admin back to the grouped slip page (or the requests list if the
    entry has no desk code)."""
    if obj.reference_code:
        return redirect("inventory:request_slip", code=obj.reference_code)
    return redirect("inventory:requests")


def _loan_for_checkin(txn):
    """Resolve the loan Request a check-in transaction was filed against, via the
    check-out it returns (source_transaction -> RequestItem -> Request)."""
    from .models import RequestItem

    checkout = txn.source_transaction
    if checkout is None:
        return None
    line = (
        RequestItem.objects.select_related("request")
        .filter(transaction=checkout)
        .first()
    )
    return line.request if line else None


@require_POST
@user_passes_test(is_superadmin)
def transaction_void(request, pk):
    """Void a single desk return: a check-in transaction filed straight against a
    loan via return-by-code (it has no return Request to void). Its units go back
    out and the loan it closed is reopened, leaving the rest of the slip intact."""
    from .models import Transaction

    txn = get_object_or_404(
        Transaction.objects.select_related("source_transaction"), pk=pk
    )
    loan = _loan_for_checkin(txn)
    if txn.is_voided:
        messages.error(request, "This entry has already been voided.")
    else:
        reason = (request.POST.get("reason") or "").strip()
        ok, error = void_transaction(txn, request.user, reason=reason)
        if not ok:
            messages.error(request, error)
        else:
            # The units are out again, so the loan is no longer fully returned.
            if loan is not None and loan.returned_at is not None:
                loan.returned_at = None
                loan.returned_by = None
                loan.returned_by_name = ""
                loan.save(update_fields=["returned_at", "returned_by", "returned_by_name"])
            messages.success(
                request, f"Voided the return on slip {loan.reference_code if loan else txn.pk}."
            )
    if loan and loan.reference_code:
        return redirect("inventory:request_slip", code=loan.reference_code)
    return redirect("inventory:requests")


@user_passes_test(lambda user: user.is_staff)
def request_lookup_code(request):
    """Lightweight AJAX lookup of a loan/reservation by its desk slip code.

    Used by the admin quick-action scanner: when an admin scans a slip QR and
    it is a book-ahead that has not been handed over yet, the client switches
    the form to check-out and submits a hand-over for the reservation. Returns
    enough to pre-fill the form (type, hand-over status, item lines)."""
    from .models import Request

    code = (request.GET.get("code") or "").strip()
    if not code:
        return JsonResponse({"found": False})
    req = (
        Request.objects.filter(reference_code__iexact=code, source_request__isnull=True)
        .select_related("user")
        .prefetch_related("items__item", "items__location")
        .first()
    )
    if req is None:
        return JsonResponse({"found": False})
    items = [
        {
            "item_id": line.item_id,
            "name": line.item.name if line.item else "",
            "quantity": line.quantity,
            "location_id": line.location_id,
        }
        for line in req.items.all()
    ]
    return JsonResponse(
        {
            "found": True,
            "pk": req.pk,
            "reference_code": req.reference_code,
            "transaction_type": req.transaction_type,
            "approval_status": req.approval_status,
            "handed_over": req.is_handed_over,
            "returned": req.is_returned,
            "voided": req.is_voided,
            "person_name": req.person_name or (req.user.username if req.user else ""),
            "expected_return": req.expected_return.isoformat() if req.expected_return else "",
            "notes": req.notes or "",
            "items": items,
        }
    )


@login_required
@user_passes_test(is_superadmin)
def request_edit(request, pk):
    """Admin edits a request before deciding, or a book-ahead reservation after
    approval (it holds no stock). Approved check-outs/ins already moved stock, so
    they are locked.

    Supports both a legacy single-item :class:`Transaction` (no Request parent)
    and the new multi-item :class:`Request` (one decision covers every line).
    For a Request, the shared fields (type, dates, notes, approval status) are
    edited and the first item line reflects the single item/quantity the form
    captures."""
    obj, is_request = _resolve_request(pk)
    if obj is None:
        messages.error(request, "That request could not be found.")
        return _redirect_after_request_action(obj)
    if obj.approval_status != "pending" and obj.transaction_type != "book_ahead":
        messages.error(request, "That request can no longer be edited.")
        return _redirect_after_request_action(obj)

    if request.method == "POST":
        form = RequestEditForm(request.POST, user=request.user)
        if form.is_valid():
            previous_status = obj.approval_status
            # Snapshot the editable fields so we can tell later whether an
            # "updated" notification is actually warranted.
            if is_request:
                first_line = obj.items.select_related("item", "location", "condition").first()
                before = {
                    "transaction_type": obj.transaction_type,
                    "item_id": first_line.item_id if first_line else None,
                    "quantity": first_line.quantity if first_line else 1,
                    "location_id": first_line.location_id if first_line else None,
                    "condition_id": first_line.condition_id if first_line else None,
                    "expected_return": obj.expected_return,
                    "taken_at": obj.taken_at,
                    "notes": obj.notes,
                }
            else:
                before = {
                    "transaction_type": obj.transaction_type,
                    "item_id": obj.item_id,
                    "quantity": obj.quantity,
                    "location_id": obj.location_id,
                    "condition_id": obj.condition_id,
                    "expected_return": obj.expected_return,
                    "taken_at": obj.taken_at,
                    "notes": obj.notes,
                }
            obj.transaction_type = form.cleaned_data["transaction_type"]
            obj.quantity = form.cleaned_data["quantity"]
            obj.expected_return = _localize(form.cleaned_data.get("expected_return"))
            obj.taken_at = _localize(form.cleaned_data.get("taken_at"))
            obj.notes = form.cleaned_data["notes"]
            # Book-ahead reservations and check-outs assume working gear,
            # so condition is not edited for them.
            row_condition = (
                None if obj.transaction_type in ("book_ahead", "check_out")
                else form.cleaned_data.get("condition")
            )
            if is_request:
                if first_line:
                    first_line.item = form.cleaned_data["item"]
                    first_line.quantity = form.cleaned_data["quantity"]
                    first_line.location = form.cleaned_data.get("location")
                    first_line.condition = row_condition
                    first_line.save()
                obj.save()
            else:
                obj.item = form.cleaned_data["item"]
                obj.location = form.cleaned_data.get("location")
                obj.condition = row_condition
                obj.save()

            # Reconcile the approval status chosen in the form.
            new_status = form.cleaned_data["approval_status"]
            status_changed = new_status != previous_status
            if status_changed:
                if new_status == "approved":
                    if obj.approval_status == "pending":
                        ok, error = apply_request(obj, decided_by=request.user)
                        if not ok:
                            messages.error(request, error)
                            return render(request, "inventory/request_edit.html", {"form": form, "req": obj, "is_request": is_request})
                elif new_status == "rejected":
                    if obj.approval_status == "pending":
                        reject_request(obj, decided_by=request.user)
                    else:
                        # Rejecting an approved book-ahead reservation (no stock moved).
                        obj.approval_status = "rejected"
                        obj.decided_by = request.user
                        obj.decided_by_name = display_name(request.user)
                        obj.decided_at = timezone.now()
                        obj.save(update_fields=["approval_status", "decided_by", "decided_by_name", "decided_at"])
                        notify_transaction_change(obj, "rejected", actor_name=display_name(request.user))
                elif new_status == "pending":
                    # Reset an approved reservation back to pending (no stock moved).
                    obj.approval_status = "pending"
                    obj.decided_by = None
                    obj.decided_by_name = ""
                    obj.decided_at = None
                    obj.save(update_fields=["approval_status", "decided_by", "decided_by_name", "decided_at"])
                    notify_transaction_change(obj, "pending", actor_name=display_name(request.user))

            # Field-only edit of a confirmed reservation/request: only notify the
            # user when something actually changed, not on a no-op re-save.
            if (
                not status_changed
                and obj.approval_status == "approved"
            ):
                if is_request:
                    after = {
                        "transaction_type": obj.transaction_type,
                        "item_id": first_line.item_id if first_line else None,
                        "quantity": first_line.quantity if first_line else 1,
                        "location_id": first_line.location_id if first_line else None,
                        "condition_id": first_line.condition_id if first_line else None,
                        "expected_return": obj.expected_return,
                        "taken_at": obj.taken_at,
                        "notes": obj.notes,
                    }
                else:
                    after = {
                        "transaction_type": obj.transaction_type,
                        "item_id": obj.item_id,
                        "quantity": obj.quantity,
                        "location_id": obj.location_id,
                        "condition_id": obj.condition_id,
                        "expected_return": obj.expected_return,
                        "taken_at": obj.taken_at,
                        "notes": obj.notes,
                    }
                if before != after:
                    notify_transaction_change(obj, "updated", actor_name=display_name(request.user))

            item_name = (
                (first_line.item.name if first_line else "request") if is_request
                else obj.item.name
            )
            messages.success(request, f"Updated request for {item_name}.")
            return _redirect_after_request_action(obj)
        messages.error(request, "Please correct the errors and try again.")
    else:
        def to_local(dt):
            if not dt:
                return None
            return timezone.localtime(dt).replace(tzinfo=None)

        if is_request:
            first_line = obj.items.select_related("item", "location", "condition").first()
            initial = {
                "transaction_type": obj.transaction_type,
                "approval_status": obj.approval_status,
                "quantity": first_line.quantity if first_line else 1,
                "location": first_line.location_id if first_line else None,
                "condition": first_line.condition_id if first_line else None,
                "expected_return": to_local(obj.expected_return),
                "taken_at": to_local(obj.taken_at),
                "notes": obj.notes,
            }
            if first_line:
                initial["item"] = first_line.item_id
        else:
            initial = {
                "transaction_type": obj.transaction_type,
                "approval_status": obj.approval_status,
                "item": obj.item_id,
                "quantity": obj.quantity,
                "location": obj.location_id,
                "condition": obj.condition_id,
                "expected_return": to_local(obj.expected_return),
                "taken_at": to_local(obj.taken_at),
                "notes": obj.notes,
            }
        form = RequestEditForm(initial=initial, user=request.user)

    return render(
        request,
        "inventory/request_edit.html",
        {"form": form, "req": obj, "is_request": is_request},
    )


@login_required
@ensure_csrf_cookie
def item_detail(request, pk):
    stock_entry = StockEntry.objects.select_related("catalog_item", "location").get(pk=pk)
    if not request.user.is_staff and not location_is_visible(request.user, stock_entry.location):
        messages.error(request, "You do not have access to that location.")
        return redirect("inventory:catalog")
    all_entries = StockEntry.objects.filter(catalog_item=stock_entry.catalog_item).select_related("catalog_item", "location", "condition", "status").order_by("location__name")
    return render(
        request,
        "inventory/item_detail.html",
        {
            "item": stock_entry,
            "all_entries": all_entries,
            "can_check_out": can_check_out(request.user),
            "cart_counts": _cart_counts(request),
        },
    )


@user_passes_test(lambda user: user.is_staff)
def item_list(request):
    stock_entries = (
        StockEntry.objects.select_related("catalog_item", "location", "condition", "status")
        .all()
        .order_by("catalog_item__category", "catalog_item__subcategory", "catalog_item__name", "location__name")
    )

    grouped = {}
    for entry in stock_entries:
        if entry.pk:
            grouped.setdefault(entry.catalog_item, []).append(entry)

    grouped_items = []
    for catalog, entries in grouped.items():
        total_available = sum(entry.quantity_available for entry in entries)
        grouped_items.append({
            "catalog": catalog,
            "entries": entries,
            "total_available": total_available,
            "first_entry": entries[0] if entries else None,
        })

    total_items = stock_entries.count()
    total_available = sum(entry.quantity_available for entry in stock_entries)
    total_out = sum(entry.quantity_out for entry in stock_entries)
    maintenance_count = Maintenance.objects.filter(completed_at__isnull=True).count()
    locations = Location.objects.filter(is_active=True)
    categories = list(
        CatalogItem.objects.exclude(category__isnull=True)
        .exclude(category="")
        .values_list("category", flat=True)
        .distinct()
        .order_by("category")
    )
    return render(
        request,
        "inventory/item_list.html",
        {
            "grouped_items": grouped_items,
            "locations": locations,
            "categories": categories,
            "total_items": total_items,
            "total_available": total_available,
            "total_out": total_out,
            "maintenance_count": maintenance_count,
            "last_updated": timezone.now(),
        },
    )


@user_passes_test(is_superadmin)
def item_create(request):
    if request.method == "POST":
        form = ItemForm(request.POST, request.FILES)
        formset = StockEntryFormSet(request.POST, user=request.user)
        if form.is_valid() and formset.is_valid():
            sku = (form.cleaned_data.get("sku") or "").strip()
            image = request.FILES.get("image") or form.cleaned_data.get("image")
            catalog, _ = CatalogItem.objects.get_or_create(
                sku=sku,
                defaults={
                    "name": form.cleaned_data.get("name"),
                    "description": form.cleaned_data.get("description") or "",
                    "category": form.cleaned_data.get("category") or "",
                    "subcategory": form.cleaned_data.get("subcategory") or "",
                    "image": image,
                    "is_active": True,
                },
            )
            if image and not catalog.image:
                catalog.image = image
                catalog.save(update_fields=["image"])
            created = []
            for stock_form in formset:
                if stock_form.cleaned_data and not stock_form.cleaned_data.get("DELETE", False):
                    location = stock_form.cleaned_data.get("location")
                    if not location:
                        continue
                    existing = StockEntry.objects.filter(catalog_item=catalog, location=location).first()
                    if existing:
                        existing.quantity_total = (existing.quantity_total or 0) + (stock_form.cleaned_data.get("quantity_total") or 0)
                        existing.quantity_out = (existing.quantity_out or 0) + (stock_form.cleaned_data.get("quantity_out") or 0)
                        existing.quantity_maintenance = (existing.quantity_maintenance or 0) + (stock_form.cleaned_data.get("quantity_maintenance") or 0)
                        for field in ("condition", "status"):
                            value = stock_form.cleaned_data.get(field)
                            if value:
                                setattr(existing, field, value)
                        existing.save()
                        created.append(existing)
                    else:
                        created.append(StockEntry.objects.create(
                            catalog_item=catalog,
                            location=location,
                            quantity_total=stock_form.cleaned_data.get("quantity_total") or 0,
                            quantity_out=stock_form.cleaned_data.get("quantity_out") or 0,
                            quantity_maintenance=stock_form.cleaned_data.get("quantity_maintenance") or 0,
                            condition=stock_form.cleaned_data.get("condition"),
                            status=stock_form.cleaned_data.get("status"),
                        ))
            if created:
                messages.success(request, f"Added {catalog.name} in {len(created)} location(s). QR codes were generated automatically.")
                return redirect("inventory:item_detail", pk=created[0].pk)
            messages.error(request, "Please add at least one location.")
    else:
        form = ItemForm()
        formset = StockEntryFormSet(queryset=StockEntry.objects.none(), user=request.user)
    return render(request, "inventory/item_form.html", {"form": form, "formset": formset, "is_edit": False})


@user_passes_test(is_superadmin)
def item_edit(request, pk):
    stock_entry = get_object_or_404(StockEntry, pk=pk)
    catalog = stock_entry.catalog_item
    if request.method == "POST":
        form = ItemForm(request.POST, request.FILES, instance=catalog)
        formset = StockEntryFormSet(request.POST, queryset=StockEntry.objects.filter(catalog_item=catalog), user=request.user)
        if form.is_valid() and formset.is_valid():
            form.save()
            for stock_form in formset:
                if stock_form.cleaned_data.get("DELETE", False):
                    if stock_form.instance.pk:
                        stock_form.instance.delete()
                    continue
                if stock_form.cleaned_data.get("location"):
                    location = stock_form.cleaned_data["location"]
                    existing = StockEntry.objects.filter(catalog_item=catalog, location=location).exclude(pk=stock_form.instance.pk).first()
                    if existing:
                        existing.quantity_total = (existing.quantity_total or 0) + (stock_form.cleaned_data.get("quantity_total") or 0)
                        existing.quantity_out = (existing.quantity_out or 0) + (stock_form.cleaned_data.get("quantity_out") or 0)
                        existing.quantity_maintenance = (existing.quantity_maintenance or 0) + (stock_form.cleaned_data.get("quantity_maintenance") or 0)
                        for field in ("condition", "status"):
                            value = stock_form.cleaned_data.get(field)
                            if value:
                                setattr(existing, field, value)
                        existing.save()
                        stock_form.instance.delete()
                    else:
                        stock_form.instance.catalog_item = catalog
                        stock_form.instance.save()
            messages.success(request, f"Updated {catalog.name}.")
            return redirect("inventory:item_detail", pk=stock_entry.pk)
    else:
        form = ItemForm(instance=catalog)
        formset = StockEntryFormSet(queryset=StockEntry.objects.filter(catalog_item=catalog), user=request.user)
    return render(request, "inventory/item_form.html", {"form": form, "formset": formset, "is_edit": True, "item": stock_entry})


@user_passes_test(lambda user: user.is_staff or role_of(user) == ROLE_TEACHER)
def maintenance_list(request):
    is_staff = bool(request.user.is_staff)
    # Teachers only ever see the maintenance they submitted.
    records_qs = Maintenance.objects.all()
    if not is_staff:
        records_qs = records_qs.filter(reported_by=request.user)
    open_records = (
        records_qs.filter(completed_at__isnull=True)
        .select_related("item", "item__catalog_item", "reported_by")
        .order_by("started_at")
    )
    completed_records = (
        records_qs.filter(completed_at__isnull=False)
        .select_related("item", "item__catalog_item", "completed_by")
        .order_by("-completed_at")[:20]
    )
    now = timezone.now()
    for record in open_records:
        record.is_overdue = bool(record.expected_return and record.expected_return < now)
    form = MaintenanceForm(request.GET or None, user=request.user)
    if request.GET.get("item"):
        try:
            form.fields["item"].initial = int(request.GET["item"])
        except (ValueError, TypeError):
            pass
    maintenance_items = (
        StockEntry.objects.filter(maintenance_records__completed_at__isnull=True)
        .filter(quantity_maintenance__gt=0)
        .select_related("catalog_item", "location", "condition", "status")
        .distinct()
        .order_by("catalog_item__category", "catalog_item__subcategory", "catalog_item__name")
    )
    return render(
        request,
        "inventory/maintenance.html",
        {
            "open_records": open_records,
            "completed_records": completed_records,
            "form": form,
            "is_staff": is_staff,
            "total_in_maintenance": open_records.count(),
            "maintenance_items": maintenance_items,
        },
    )


@login_required
def maintenance_detail(request, pk):
    record = get_object_or_404(
        Maintenance.objects.select_related("item", "item__catalog_item", "reported_by", "completed_by"),
        pk=pk,
    )
    # Teachers may only view their own submissions; staff may view any.
    if not is_staff_role(request.user) and record.reported_by_id != request.user.id:
        messages.error(request, "You can only view maintenance you submitted.")
        return redirect("inventory:maintenance")
    now = timezone.now()
    record.is_overdue = bool(record.expected_return and not record.completed_at and record.expected_return < now)
    return render(
        request,
        "inventory/maintenance_detail.html",
        {
            "record": record,
            "is_staff": bool(request.user.is_staff),
            "now": now,
        },
    )


@require_POST
@user_passes_test(lambda user: user.is_staff or role_of(user) == ROLE_TEACHER)
def maintenance_start(request):
    form = MaintenanceForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Please choose an item and try again.")
        return redirect("inventory:maintenance")
    item = form.cleaned_data["item"]
    quantity = form.cleaned_data.get("quantity") or 1
    record = set_item_maintenance(
        item,
        user=request.user,
        reason=form.cleaned_data.get("reason", ""),
        expected_return=form.cleaned_data.get("expected_return"),
        quantity=quantity,
        location=form.cleaned_data.get("location"),
    )
    messages.success(request, f"{quantity} x {item.name} moved to maintenance.")
    return redirect("inventory:maintenance")


@require_POST
@user_passes_test(lambda user: user.is_staff)
def maintenance_complete(request, pk):
    record = get_object_or_404(Maintenance, pk=pk, completed_at__isnull=True)
    complete_item_maintenance(record, user=request.user, notes=request.POST.get("notes", ""))
    messages.success(request, f"{record.item.name} returned from maintenance.")
    return redirect("inventory:maintenance")


@require_POST
@user_passes_test(lambda user: user.is_staff)
def maintenance_write_off(request, pk):
    record = get_object_or_404(Maintenance, pk=pk, completed_at__isnull=True)
    write_off_maintenance(record, user=request.user, notes=request.POST.get("notes", ""))
    messages.success(request, f"{record.item.name} written off (could not be fixed).")
    return redirect("inventory:maintenance")


def live_version(request):
    """Current data version. Clients poll this and refresh when it changes."""
    from . import live

    return JsonResponse({"version": live.peek_version()})


def live_state(request):
    """Lightweight poll endpoint returning the data version plus the dynamic
    chrome values shown in the top bar (notification / request / cart badges).

    Returning these as JSON lets the client update the badges *in place* the
    moment a change is pushed, without swapping ``#main`` and blowing away any
    form the user has half-filled. The heavier ``#main`` refresh (with full
    context preservation) still happens on a version bump for the page body."""
    from django.db.models import Q
    from django.utils import timezone

    from . import live
    from .context_processors import _cart_counts
    from .models import Announcement, Notification, Transaction

    user = getattr(request, "user", None)
    data = {"version": live.peek_version()}

    if user and user.is_authenticated:
        now = timezone.now()
        data["unread_notification_count"] = Notification.objects.filter(
            user=user, is_read=False
        ).count()
        data["announcement_count"] = (
            Announcement.objects.filter(is_active=True)
            .filter(Q(visible_until__isnull=True) | Q(visible_until__gte=now))
            .count()
        )
        data["pending_request_count"] = (
            Transaction.objects.filter(approval_status="pending", voided="none").count()
            if user.is_staff
            else 0
        )
        data["cart_total"] = _cart_counts(request)["total"]
    else:
        data["unread_notification_count"] = 0
        data["announcement_count"] = 0
        data["pending_request_count"] = 0
        data["cart_total"] = 0

    return JsonResponse(data)


def live_region(request):
    """Return the dynamic regions of a page as standalone HTML fragments.

    The client polls this (passing the path it is currently on) and swaps each
    returned fragment into the matching container *in place* — no full page
    reload, so half-filled forms, scroll position, open modals and camera
    panels are never disturbed by a pushed change. The fragments are produced
    by re-running the page's own view with identical context, so there is a
    single source of truth (no duplicated queryset logic)."""
    from django.http import HttpResponse, QueryDict
    from django.urls import Resolver404, resolve

    from . import live

    raw_path = request.GET.get("path") or request.path
    query_string = ""
    if isinstance(raw_path, str) and "?" in raw_path:
        path_only, query_string = raw_path.split("?", 1)
    else:
        path_only = raw_path

    try:
        match = resolve(path_only)
    except Resolver404:
        return JsonResponse({"version": live.peek_version(), "regions": {}}, status=404)

    # Re-run the resolved view on a request that carries the same auth/session
    # context, so permission gating and rendered data match the live page.
    view_func = match.func
    new_request = request
    new_request.resolver_match = match

    # Merge any query-string params the client sent (e.g. catalog filters) so
    # the view renders the same filtered data the user is currently seeing.
    if query_string:
        try:
            qd = QueryDict(query_string)
            merged = request.GET.copy()
            for key in qd:
                merged.setlist(key, qd.getlist(key))
            new_request.GET = merged
        except Exception:
            pass

    try:
        response = view_func(new_request, *match.args, **match.kwargs)
    except Exception:
        # If the view rejects the synthetic call (e.g. a non-GET only view),
        # fall back to whatever we can. Never crash the poll loop.
        return JsonResponse({"version": live.peek_version(), "regions": {}})

    if not isinstance(response, HttpResponse):
        return JsonResponse({"version": live.peek_version(), "regions": {}})

    html = response.content.decode(response.charset)
    regions = _extract_regions(html)

    # Drop any region whose container currently holds the focused input — we
    # must not yank the field out from under the user. The client passes the
    # id of the [data-region] ancestor of the focused element (or empty).
    focused = (request.GET.get("focused") or "").strip()
    if focused and focused in regions:
        regions.pop(focused, None)

    # Also drop any region the client reports as actively being interacted
    # with (a hovered region or an expanded catalog group) so the swap cannot
    # disturb the user even on a slow client. Client still double-checks.
    skip = (request.GET.get("skip") or "").strip()
    if skip:
        for rid in skip.split(","):
            rid = rid.strip()
            if rid:
                regions.pop(rid, None)

    return JsonResponse({"version": live.peek_version(), "regions": regions})


def _extract_regions(html):
    """Pull every element carrying ``data-region`` out of ``html`` and return
    ``{id: outerHTML}``. Pure stdlib (html.parser) — no extra deps."""
    from html.parser import HTMLParser

    class _RegionExtractor(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []          # list of dicts for open region-tagged elements
            self.regions = {}        # id -> list of html chunks
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            region_id = d.get("data-region")
            node = {"tag": tag, "attrs": attrs, "region": region_id, "children": []}
            self.depth += 1
            if self.stack:
                self.stack[-1]["children"].append(node)
            self.stack.append(node)

        def handle_startendtag(self, tag, attrs):
            d = dict(attrs)
            node = {"tag": tag, "attrs": attrs, "region": d.get("data-region"), "children": [], "void": True}
            if self.stack:
                self.stack[-1]["children"].append(node)

        def handle_endtag(self, tag):
            if not self.stack:
                return
            # Pop until we match the tag (tolerate malformed nesting).
            while self.stack:
                top = self.stack.pop()
                if top["tag"] == tag:
                    break
            # If the popped node was a region root, record it.
            if top.get("region"):
                rid = top["region"]
                self.regions.setdefault(rid, []).append(_serialize(top))

        def handle_data(self, data):
            if self.stack:
                self.stack[-1]["children"].append({"text": data})

    parser = _RegionExtractor()
    parser.feed(html)
    out = {}
    for rid, chunks in parser.regions.items():
        out[rid] = "".join(chunks)
    return out


def _serialize(node):
    if "text" in node:
        return node["text"]
    tag = node["tag"]
    attrs = "".join(' {}="{}"'.format(k, _escape_attr(v)) for k, v in node["attrs"])
    inner = "".join(_serialize(c) for c in node.get("children", []))
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
    if tag in VOID and not inner:
        return "<{0}{1}>".format(tag, attrs)
    return "<{0}{1}>{2}</{0}>".format(tag, attrs, inner)


def _escape_attr(value):
    if value is None:
        return ""
    return (str(value).replace("&", "&amp;").replace('"', "&quot;"))


@user_passes_test(lambda user: user.is_staff)
def stock_take_list(request):
    """Overview page for stock takes: stats + filtered list + create button."""
    from django.db.models import Sum

    all_takes = StockTake.objects.select_related("location", "taken_by").all()
    takes = all_takes.order_by("-taken_at")
    location_filter = request.GET.get("location", "")
    status_filter = request.GET.get("status", "")
    if location_filter:
        takes = takes.filter(location__name__iexact=location_filter)
    if status_filter:
        takes = takes.filter(status=status_filter)
    locations = Location.objects.filter(is_active=True).order_by("name")

    total_takes = all_takes.count()
    complete_count = all_takes.filter(status="complete").count()
    draft_count = all_takes.filter(status="draft").count()
    total_items_counted = (
        StockTakeItem.objects.filter(stock_take__status="complete")
        .aggregate(total=Sum("counted_quantity"))["total"] or 0
    )
    from django.db.models import F
    total_discrepancy = (
        StockTakeItem.objects.filter(stock_take__status="complete")
        .aggregate(discrepancy=Sum(F("counted_quantity") - F("expected_quantity")))["discrepancy"] or 0
    )
    recent_takes = all_takes.order_by("-taken_at")[:5]

    return render(
        request,
        "inventory/stock_take_list.html",
        {
            "stock_takes": takes,
            "locations": locations,
            "location_filter": location_filter,
            "status_filter": status_filter,
            "total_takes": total_takes,
            "complete_count": complete_count,
            "draft_count": draft_count,
            "total_items_counted": total_items_counted,
            "total_discrepancy": total_discrepancy,
            "recent_takes": recent_takes,
        },
    )


@user_passes_test(is_superadmin)
def stock_take_create(request):
    """Create a new stock take in two steps:
    Step 1: Choose a location.
    Step 2: Count items at that location and submit.
    """
    if request.method == "POST":
        step = request.POST.get("step", "1")
        if step == "1":
            location_id = request.POST.get("location_id", "").strip()
            if not location_id:
                messages.error(request, "Please select a location.")
                return redirect("inventory:stock_take_create")
            try:
                location = Location.objects.get(pk=int(location_id))
            except (ValueError, Location.DoesNotExist):
                messages.error(request, "Invalid location.")
                return redirect("inventory:stock_take_create")
            request.session["stock_take_location_id"] = location.pk
            request.session["stock_take_location_name"] = location.name
            return redirect("inventory:stock_take_create")
        elif step == "2":
            location_id = request.session.get("stock_take_location_id")
            location_name = request.session.get("stock_take_location_name", "")
            if not location_id:
                messages.error(request, "No location selected. Start again.")
                return redirect("inventory:stock_take_create")
            location = get_object_or_404(Location, pk=location_id)
            notes = request.POST.get("notes", "").strip()
            item_ids = request.POST.getlist("item_ids")
            counted_quantities = request.POST.getlist("counted_quantity")
            if not item_ids:
                messages.error(request, "No items to count. Go back and select items.")
                return redirect("inventory:stock_take_create")
            stock_take = StockTake.objects.create(
                location=location,
                taken_by=request.user,
                notes=notes,
                status="draft",
            )
            for index, item_id in enumerate(item_ids):
                if not item_id.strip():
                    continue
                try:
                    item = StockEntry.objects.get(pk=int(item_id))
                except (ValueError, StockEntry.DoesNotExist):
                    continue
                counted = 1
                if index < len(counted_quantities) and counted_quantities[index].strip():
                    try:
                        counted = max(0, int(counted_quantities[index]))
                    except ValueError:
                        counted = 0
                StockTakeItem.objects.create(
                    stock_take=stock_take,
                    item=item,
                    counted_quantity=counted,
                    expected_quantity=item.quantity_total,
                    notes="",
                )
            stock_take.status = "complete"
            stock_take.save()
            # Adjust stock entry quantities to match the counted values.
            for sti in stock_take.items.all():
                diff = sti.counted_quantity - sti.expected_quantity
                if diff != 0:
                    sti.item.quantity_total = sti.counted_quantity
                    sti.item.save()
            del request.session["stock_take_location_id"]
            del request.session["stock_take_location_name"]
            messages.success(request, f"Stock take for {location_name} completed. {stock_take.item_count} item(s) counted.")
            return redirect("inventory:stock_take_detail", pk=stock_take.pk)
    # Step 1 or initial load.
    # A "change_location" GET param clears any stale session so the user
    # can pick a different location instead of being stuck on the previous one.
    if request.GET.get("change_location"):
        request.session.pop("stock_take_location_id", None)
        request.session.pop("stock_take_location_name", None)
        return redirect("inventory:stock_take_create")
    location_id = request.session.get("stock_take_location_id")
    location_name = request.session.get("stock_take_location_name", "")
    locations = Location.objects.filter(is_active=True).order_by("name")
    items = []
    if location_id:
        try:
            items = StockEntry.objects.filter(location_id=location_id, is_active=True).select_related("catalog_item").order_by("catalog_item__name")
        except ValueError:
            pass
    return render(
        request,
        "inventory/stock_take_form.html",
        {
            "locations": locations,
            "selected_location_id": location_id,
            "selected_location_name": location_name,
            "items": items,
            "step": 2 if location_id else 1,
        },
    )


@user_passes_test(lambda user: user.is_staff)
def stock_take_detail(request, pk):
    """View a stock take's details."""
    stock_take = get_object_or_404(
        StockTake.objects.select_related("location", "taken_by"),
        pk=pk,
    )
    take_items = stock_take.items.select_related("item").all()
    return render(
        request,
        "inventory/stock_take_detail.html",
        {
            "stock_take": stock_take,
            "take_items": take_items,
        },
    )


@user_passes_test(lambda user: user.is_staff)
def stock_take_print(request):
    """Print current stock levels for a selected location or all locations."""
    locations = Location.objects.filter(is_active=True).order_by("name")
    location_id = request.GET.get("location", "")

    items = (
        StockEntry.objects.select_related("catalog_item", "location", "condition", "status")
        .filter(is_active=True)
        .order_by("location__name", "catalog_item__name")
    )

    selected_location = None
    if location_id:
        try:
            selected_location = Location.objects.get(pk=int(location_id), is_active=True)
            items = items.filter(location=selected_location)
        except (ValueError, Location.DoesNotExist):
            selected_location = None

    grouped = {}
    for item in items:
        loc_name = item.location.name
        grouped.setdefault(loc_name, []).append(item)

    return render(
        request,
        "inventory/stock_take_print.html",
        {
            "locations": locations,
            "selected_location": selected_location,
            "grouped": grouped,
            "items": items,
            "printed_at": timezone.now(),
        },
    )


@login_required
@require_POST
def onesignal_subscribe(request):
    """Register or update a OneSignal subscription ID for the current user."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)

    subscription_id = (payload.get("subscription_id") or "").strip()
    user_agent = (payload.get("userAgent") or request.META.get("HTTP_USER_AGENT", "")).strip()[:512]
    if not subscription_id:
        return JsonResponse({"ok": False, "error": "Missing subscription_id."}, status=400)

    OneSignalPlayer.objects.update_or_create(
        player_id=subscription_id,
        defaults={
            "user": request.user,
            "user_agent": user_agent,
            "last_used": timezone.now(),
        },
    )
    return JsonResponse({"ok": True, "status": "subscribed"})


@login_required
@require_POST
def onesignal_unsubscribe(request):
    """Remove a OneSignal subscription ID for the current user."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)

    subscription_id = (payload.get("subscription_id") or "").strip()
    if not subscription_id:
        return JsonResponse({"ok": False, "error": "Missing subscription_id."}, status=400)

    deleted, _ = OneSignalPlayer.objects.filter(player_id=subscription_id, user=request.user).delete()
    return JsonResponse({"ok": True, "deleted": deleted})


@login_required
def onesignal_devices(request):
    """List the current user's OneSignal subscription IDs (JSON)."""
    devices = OneSignalPlayer.objects.filter(user=request.user).order_by("-created_at")
    data = [
        {
            "subscription_id": d.player_id,
            "user_agent": d.user_agent,
            "created_at": d.created_at.isoformat(),
            "last_used": d.last_used.isoformat(),
        }
        for d in devices
    ]
    return JsonResponse({"ok": True, "devices": data})


@require_GET
def onesignal_sw(request):
    sw_path = settings.BASE_DIR / "static" / "OneSignalSDKWorker.js"
    return FileResponse(open(sw_path, "rb"), content_type="application/javascript")

