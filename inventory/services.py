"""Business logic for stock movements.

Centralising check-out / check-in / booking here keeps the views thin and
guarantees the quantity invariant (available = total - out) is enforced in one
place, inside a database transaction with a row lock to prevent overselling
under concurrent requests.
"""
import datetime

from django.db import models, transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.timesince import timeuntil, timesince

from .models import (
    STATUS_AVAILABLE,
    STATUS_LATE_RETURN,
    STATUS_MAINTENANCE,
    STATUS_OUT,
    Item,
    Maintenance,
    Notification,
    Transaction,
    display_name,
    get_status_option,
    notify_transaction_change,
)


def _resolve_status(item):
    """Derive the correct StatusOption for `item` from its current quantities
    (and, when out, whether any live check-out is overdue).

    Centralising this means status can never drift out of sync with the
    quantity fields the way hand-set values used to: every call site that
    changes quantity_out / quantity_maintenance should set status via this
    function instead of hardcoding a value. Priority when more than one is
    true: Out / Late Return > Maintenance > Available, since "some units are
    still with a member" is the most operationally relevant fact.
    """
    if item.quantity_out > 0:
        overdue = item.transactions.filter(
            transaction_type="check_out",
            approval_status="approved",
            voided="none",
            expected_return__isnull=False,
            expected_return__lt=timezone.now(),
        ).exists()
        return get_status_option(STATUS_LATE_RETURN if overdue else STATUS_OUT)
    if item.quantity_maintenance > 0:
        return get_status_option(STATUS_MAINTENANCE)
    return get_status_option(STATUS_AVAILABLE)


def _resolve_source_checkout(item, user):
    """Find the still-outstanding check-out a check-in should be attributed to.

    When a check-in isn't told which loan it closes, attribute it FIFO to the
    earliest approved, non-voided check-out for the item. Reservations (loans
    spawned at hand-over) are preferred so returning a book-ahead's own gear is
    credited to that reservation rather than an unrelated loan of the same item.
    """
    from .models import Request, RequestItem, Transaction

    def _outstanding(checkout):
        returned = Transaction.objects.filter(
            source_transaction=checkout,
            transaction_type="check_in",
            approval_status="approved",
            voided="none",
        ).aggregate(total=Sum("quantity"))["total"] or 0
        return checkout.quantity - returned

    def _ordered(candidates):
        # Prefer this member's own loans (never attribute a return to another
        # member's loan), then reservation-sourced loans, then most-recent
        # first so a quick check-out/check-in of the same item pairs with the
        # loan the member just took rather than an older outstanding one.
        def _key(c):
            is_own = c.user_id == user.id
            is_reservation = RequestItem.objects.filter(
                transaction=c, request__transaction_type="book_ahead"
            ).exists()
            return (0 if is_own else 1, 0 if is_reservation else 1, -c.created_at.timestamp())
        return sorted(candidates, key=_key)

    candidates = list(Transaction.objects.filter(
        item=item,
        transaction_type="check_out",
        approval_status="approved",
        voided="none",
    ))
    for checkout in _ordered(candidates):
        if _outstanding(checkout) > 0:
            return checkout
    return None


def find_loan_request_for_return(item, user):
    """Return the open loan (check-out / book-ahead) Request a member's check-in
    of ``item`` should be filed against, or None when there is no outstanding
    loan. Resolves the still-outstanding check-out via _resolve_source_checkout
    and walks back to the Request that owns it, so the return reuses that loan's
    slip code."""
    from .models import RequestItem

    checkout = _resolve_source_checkout(item, user)
    if not checkout:
        return None
    line = RequestItem.objects.filter(transaction=checkout).select_related("request").first()
    return line.request if line else None


def _close_reservations_on_return(item):
    """When gear from a live loan is checked back in, mark that loan returned
    once every unit it released is back.

    A live loan is either a direct check-out (stock left when the request was
    approved) or a handed-over book-ahead (the reservation became a live
    check-out at pickup). Either way its loan lines carry a linked check-out
    Transaction, and a check-in records the exact check-out it returns via
    ``source_transaction``, so only units belonging to this loan count — an
    unrelated check-in (e.g. from a separate check-out of the same item) must
    not close it.

    Book-ahead reservations whose spawned check-out link is missing (legacy
    data) fall back to counting the member's check-ins for the item since
    hand-over.
    """
    from .models import Request, Transaction

    # Loans (never return requests) whose check-out for this item is live: a
    # direct check-out, or a handed-over book-ahead. Both have a linked,
    # approved check-out Transaction; a book-ahead not yet handed over does not,
    # so it is excluded naturally.
    linked = Request.objects.filter(
        voided="none",
        approval_status="approved",
        returned_at__isnull=True,
        source_request__isnull=True,
        items__transaction__item=item,
        items__transaction__transaction_type="check_out",
        items__transaction__approval_status="approved",
        items__transaction__voided="none",
    ).distinct()
    # Reservations handed over for this item but without a linked check-out
    # (legacy data) — matched separately below.
    legacy = Request.objects.filter(
        transaction_type="book_ahead",
        voided="none",
        approval_status="approved",
        handed_over_at__isnull=False,
        returned_at__isnull=True,
        items__item=item,
    ).exclude(
        items__transaction__transaction_type="check_out",
        items__transaction__approval_status="approved",
        items__transaction__voided="none",
    ).distinct()

    for reservation in linked:
        fully_returned = True
        returner = None
        for line in reservation.items.select_related("transaction").all():
            checkout = line.transaction
            if not checkout:
                fully_returned = False
                break
            returns = Transaction.objects.filter(
                source_transaction=checkout,
                transaction_type="check_in",
                approval_status="approved",
                voided="none",
            )
            if returns.exists():
                # The admin who approved the check-in is the one who returned it.
                first_return = returns.order_by("created_at").first()
                returner = first_return.decided_by
            returned = returns.aggregate(total=Sum("quantity"))["total"] or 0
            if returned < checkout.quantity:
                fully_returned = False
                break
        if fully_returned:
            reservation.returned_at = timezone.now()
            reservation.returned_by = returner
            reservation.returned_by_name = display_name(returner) if returner else ""
            reservation.save(update_fields=["returned_at", "returned_by", "returned_by_name"])

    for reservation in legacy:
        out_qty = sum(
            line.quantity for line in reservation.items.select_related("item").all()
        )
        if out_qty == 0:
            continue
        returns = Transaction.objects.filter(
            item_id__in=[l.item_id for l in reservation.items.all() if l.item_id],
            transaction_type="check_in",
            approval_status="approved",
            voided="none",
            user=reservation.user,
            created_at__gte=reservation.handed_over_at,
        )
        if returns.count() >= out_qty:
            reservation.returned_at = timezone.now()
            first_return = returns.order_by("created_at").first()
            returner = first_return.decided_by if first_return else None
            reservation.returned_by = returner
            reservation.returned_by_name = display_name(returner) if returner else ""
            reservation.save(update_fields=["returned_at", "returned_by", "returned_by_name"])


@db_transaction.atomic
def record_item_transaction(
    item,
    transaction_type,
    quantity,
    *,
    user,
    condition=None,
    location=None,
    notes="",
    expected_return=None,
    taken_at=None,
    decided_by=None,
    source_transaction=None,
):
    # Lock the row so concurrent check-outs can't both read the same available
    # count and oversell the item.
    item = Item.objects.select_for_update().get(pk=item.pk)
    quantity = max(1, int(quantity))

    if transaction_type == "check_out":
        if item.quantity_available < quantity:
            return False, f"Not enough available stock for {item.name}."
        item.quantity_out += quantity
        item.status = _resolve_status(item)
    elif transaction_type == "check_in":
        if item.quantity_out < quantity:
            return False, f"Not enough checked-out stock to return {item.name}."
        item.quantity_out -= quantity
        item.status = _resolve_status(item)
        # Link the check-in to the loan it returns (oldest still-outstanding
        # check-out for this item/member first), so returns can be tied back to
        # the exact reservation / request without cross-contaminating others.
        if source_transaction is None:
            source_transaction = _resolve_source_checkout(item, user)
    elif transaction_type == "book_ahead":
        # A booking records intent but does not decrement availability yet, so
        # stock can still be checked out in the meantime.
        pass
    else:
        return False, "Unsupported transaction type."

    update_fields = ["quantity_out"]
    if transaction_type != "book_ahead":
        update_fields.append("status")
    item.save(update_fields=update_fields)

    if transaction_type == "book_ahead":
        transaction_status = item.status or get_status_option(STATUS_AVAILABLE)
    else:
        transaction_status = item.status

    txn = Transaction.objects.create(
        item=item,
        user=user,
        transaction_type=transaction_type,
        quantity=quantity,
        condition=condition,
        status=transaction_status,
        notes=notes,
        location=location,
        expected_return=_localize(expected_return),
        taken_at=_localize(taken_at),
        approval_status="approved",
        decided_by=decided_by,
        decided_at=timezone.now() if decided_by else None,
        source_transaction=source_transaction,
    )
    # Checking gear back in may close out a handed-over book-ahead reservation.
    if transaction_type == "check_in":
        _close_reservations_on_return(item)
    return txn, None


@db_transaction.atomic
def create_pending_request(
    item,
    transaction_type,
    quantity,
    *,
    user,
    condition=None,
    location=None,
    notes="",
    expected_return=None,
    taken_at=None,
):
    """Create a request raised by a non-admin user.

    The request is parked as "pending": no stock is moved until an admin
    approves it via apply_request(). A check-out request still validates that
    enough stock exists at submission time, but the real guard runs again at
    approval.
    """
    item = Item.objects.select_for_update().get(pk=item.pk)
    quantity = max(1, int(quantity))

    if transaction_type == "check_out":
        if item.quantity_available < quantity:
            return None, f"Not enough available stock for {item.name}."

    # Book-aheads are reservations (no stock moved); check-out/in wait for the
    # admin to approve, so nothing is decremented/incremented here.
    transaction_status = item.status or get_status_option(STATUS_AVAILABLE)
    txn = Transaction.objects.create(
        item=item,
        user=user,
        transaction_type=transaction_type,
        quantity=quantity,
        condition=condition,
        status=transaction_status,
        notes=notes,
        location=location,
        expected_return=_localize(expected_return),
        taken_at=_localize(taken_at),
        approval_status="pending",
    )
    return txn, None


@db_transaction.atomic
def apply_transaction(txn, decided_by):
    """Approve a single pending Transaction (legacy single-item path)."""
    if txn.approval_status != "pending":
        return False, "This request has already been decided."

    item = Item.objects.select_for_update().get(pk=txn.item.pk)
    quantity = max(1, int(txn.quantity))

    if txn.transaction_type == "check_out":
        if item.quantity_available < quantity:
            return False, f"Not enough available stock for {item.name}."
        item.quantity_out += quantity
        item.status = _resolve_status(item)
        item.save(update_fields=["quantity_out", "status"])
    elif txn.transaction_type == "check_in":
        if item.quantity_out < quantity:
            return False, f"Not enough checked-out stock to return {item.name}."
        item.quantity_out -= quantity
        item.status = _resolve_status(item)
        item.save(update_fields=["quantity_out", "status"])
    elif txn.transaction_type == "book_ahead":
        # Approved book-ahead is a reservation only; stock moves at pickup.
        pass
    else:
        return False, "Unsupported transaction type."

    txn.approval_status = "approved"
    txn.decided_by = decided_by
    txn.decided_by_name = display_name(decided_by)
    txn.decided_at = timezone.now()
    txn.save(update_fields=["approval_status", "decided_by", "decided_by_name", "decided_at"])
    notify_transaction_change(txn, "approved", actor_name=display_name(decided_by))
    return True, None


@db_transaction.atomic
def reject_transaction(txn, decided_by):
    """Reject a single pending Transaction (legacy single-item path)."""
    if txn.approval_status != "pending":
        return False, "This request has already been decided."
    txn.approval_status = "rejected"
    txn.decided_by = decided_by
    txn.decided_by_name = display_name(decided_by)
    txn.decided_at = timezone.now()
    txn.save(update_fields=["approval_status", "decided_by", "decided_by_name", "decided_at"])
    notify_transaction_change(txn, "rejected", actor_name=display_name(decided_by))
    return True, None


def apply_request(obj, decided_by):
    """Approve a pending request. Accepts either a legacy single-item
    :class:`Transaction` (``obj.request`` is None) or a multi-item
    :class:`Request`. Stock is only moved here, for check-out/in."""
    from .models import Request

    if isinstance(obj, Request):
        return _apply_request_obj(obj, decided_by)
    return apply_transaction(obj, decided_by)


def reject_request(obj, decided_by):
    """Reject a pending request. Accepts either a :class:`Transaction` or a
    :class:`Request`."""
    from .models import Request

    if isinstance(obj, Request):
        return _reject_request_obj(obj, decided_by)
    return reject_transaction(obj, decided_by)


@db_transaction.atomic
def void_transaction(txn, decided_by, *, reason=""):
    """Cancel a transaction made by a user (wrong entry, duplicate, etc.).

    The row is kept for audit but marked voided and excluded from stock math.
    Stock is reversed for any movement the transaction already caused: an
    approved check-out returns its units to available, an approved check-in
    re-decrements them (it had incremented available). Book-aheads hold no
    stock, so nothing is reversed. The affected member is notified.
    """
    if txn.is_voided:
        return False, "This transaction has already been voided."

    item = Item.objects.select_for_update().get(pk=txn.item.pk)

    update_fields = ["quantity_out"]
    if txn.transaction_type == "check_out" and txn.approval_status == "approved":
        # Units went out on approval; bring them back.
        item.quantity_out = max(0, item.quantity_out - txn.quantity)
    elif txn.transaction_type == "check_in" and txn.approval_status == "approved":
        # Approval incremented available stock; undo that by putting the units
        # back out (capped so we never exceed total).
        item.quantity_out = min(item.quantity_total, item.quantity_out + txn.quantity)

    item.status = _resolve_status(item)
    item.save(update_fields=update_fields + ["status"])

    txn.voided = "voided"
    txn.voided_by = decided_by
    txn.voided_by_name = display_name(decided_by)
    txn.voided_at = timezone.now()
    txn.voided_reason = reason or ""
    txn.save(
        update_fields=[
            "voided",
            "voided_by",
            "voided_by_name",
            "voided_at",
            "voided_reason",
        ]
    )
    notify_transaction_change(txn, "voided", actor_name=display_name(decided_by))
    return True, None


def create_request(
    user,
    transaction_type,
    items,
    *,
    notes="",
    expected_return=None,
    taken_at=None,
    source_request=None,
):
    """Create a single multi-item Request (one admin approval covers all lines).

    ``items`` is a list of dicts:
        {"item": Item, "quantity": int, "location": Location|None, "condition": ConditionOption|None}
    Each line is validated for available stock at submit time (per item), but no
    stock is moved yet — that happens when an admin approves the request.

    When ``source_request`` is given (a check-in closing an existing loan), the
    new request reuses that loan's reference code and links back to it, so the
    return is filed under the same desk slip instead of minting a fresh code.
    """
    from .models import Request, RequestItem

    if not items:
        return None, "Please choose at least one item."
    request = Request.objects.create(
        user=user,
        person_name=display_name(user),
        transaction_type=transaction_type,
        notes=notes or "",
        expected_return=_localize(expected_return),
        taken_at=_localize(taken_at),
        approval_status="pending",
        source_request=source_request,
        reference_code=source_request.reference_code if source_request else None,
    )
    created = []
    for entry in items:
        item = entry["item"]
        quantity = max(1, int(entry.get("quantity", 1)))
        row_location = entry.get("location") or item.location
        row_condition = (
            entry.get("condition")
            if transaction_type not in ("book_ahead", "check_out")
            else None
        )
        if transaction_type == "check_out" and item.quantity_available < quantity:
            request.delete()
            return None, f"Not enough available stock for {item.name}."
        RequestItem.objects.create(
            request=request,
            item=item,
            quantity=quantity,
            location=row_location,
            condition=row_condition,
        )
        created.append((item, quantity))
    # The desk slip is shown on-screen as a popup right after submission. The
    # member chooses whether to email or print it from there, so no email is
    # sent automatically here.
    return request, None


@db_transaction.atomic
def _apply_request_obj(request_obj, decided_by):
    """Approve a multi-item Request. Each line materialises into its own
    Transaction so stock moves per item, but they all share this one decision."""
    if request_obj.approval_status != "pending":
        return False, "This request has already been decided."
    for line in request_obj.items.select_related("item").all():
        item = Item.objects.select_for_update().get(pk=line.item.pk)
        quantity = max(1, int(line.quantity))
        # Re-check stock at approval (mirrors the per-item guard).
        if request_obj.transaction_type == "check_out" and item.quantity_available < quantity:
            return False, f"Not enough available stock for {item.name}."
        # A check-in filed against a loan must credit that exact loan's checkout
        # transaction, never the FIFO guesser. This is what keeps the chosen
        # slip code's return tied to the right loan.
        source_txn = None
        if request_obj.transaction_type == "check_in" and request_obj.source_request_id:
            src_line = request_obj.source_request.items.filter(item=item).first()
            source_txn = src_line.transaction if src_line else None
        txn, error = record_item_transaction(
            item,
            request_obj.transaction_type,
            quantity,
            user=request_obj.user,
            condition=line.condition,
            location=line.location,
            notes=request_obj.notes,
            expected_return=request_obj.expected_return,
            taken_at=request_obj.taken_at,
            decided_by=decided_by,
            source_transaction=source_txn,
        )
        if error:
            return False, error
        line.transaction = txn
        line.save(update_fields=["transaction"])
    request_obj.approval_status = "approved"
    request_obj.decided_by = decided_by
    request_obj.decided_by_name = display_name(decided_by)
    request_obj.decided_at = timezone.now()
    request_obj.save(
        update_fields=["approval_status", "decided_by", "decided_by_name", "decided_at"]
    )
    notify_transaction_change(request_obj, "approved", actor_name=display_name(decided_by))
    return True, None


@db_transaction.atomic
def hand_over_request(request_obj, decided_by):
    """Hand an approved book-ahead reservation over to the member at pickup.

    A book-ahead is approved as a *reservation*: no stock moves at approval, it
    is merely held. This step is the actual pick-up — it converts the reservation
    into a live check-out, moving stock out (one Transaction per line, linked back
    to its RequestItem) and recording who handed it over and when. It is idempotent
    per request: a reservation already handed over is not moved twice.
    """
    if request_obj.transaction_type != "book_ahead":
        return False, "Only book-aheads can be handed over at pickup."
    if request_obj.approval_status != "approved":
        return False, "The reservation must be approved before it can be handed over."
    if request_obj.is_handed_over:
        return False, "This reservation has already been handed over."

    for line in request_obj.items.select_related("item", "location").all():
        item = Item.objects.select_for_update().get(pk=line.item.pk)
        quantity = max(1, int(line.quantity))
        if item.quantity_available < quantity:
            return False, f"Not enough available stock for {item.name} to hand over."
        txn, error = record_item_transaction(
            item,
            "check_out",
            quantity,
            user=request_obj.user,
            location=line.location,
            notes=request_obj.notes,
            expected_return=request_obj.expected_return,
            taken_at=request_obj.taken_at or timezone.now(),
            decided_by=decided_by,
        )
        if error:
            return False, error
        line.transaction = txn
        line.save(update_fields=["transaction"])

    request_obj.handed_over_by = decided_by
    request_obj.handed_over_by_name = display_name(decided_by)
    request_obj.handed_over_at = timezone.now()
    request_obj.save(
        update_fields=["handed_over_by", "handed_over_by_name", "handed_over_at"]
    )
    notify_transaction_change(request_obj, "handed_over", actor_name=display_name(decided_by))
    return True, None


@db_transaction.atomic
def return_request_by_code(reference_code, item_entries, *, decided_by, note=""):
    """Return gear against a specific request using its desk slip code.

    This is the precise return path: each returned item is linked to the exact
    check-out spawned by that request's hand-over (via ``source_transaction``),
    so returns never get mis-attributed to an unrelated loan of the same item.
    The request's status flips to "Returned" automatically once every line's
    loan is fully back (handled by ``_close_reservations_on_return``).

    ``item_entries`` is a list of ``{"item": Item, "quantity": int}``. An
    optional ``note`` is appended to each check-in transaction's notes (e.g.
    "Checked in by Admin") so the audit trail records who handled the return.
    """
    from .models import Request

    if not reference_code:
        return False, "Enter the request's slip code."
    # Resolve to the loan (a return request reuses the loan's code), never to a
    # child return request.
    request_obj = Request.objects.filter(
        reference_code__iexact=reference_code.strip(), source_request__isnull=True
    ).first()
    if request_obj is None:
        return False, "No request found with that code."
    if request_obj.is_voided:
        return False, "That request has been voided."
    if request_obj.transaction_type not in ("book_ahead", "check_out"):
        return False, "Only check-outs and book-aheads can be returned here."
    if not request_obj.is_handed_over and request_obj.transaction_type == "book_ahead":
        return False, "That reservation hasn't been handed over yet."
    if request_obj.is_returned:
        return False, "That request has already been returned."

    if not item_entries:
        return False, "No items were provided to return."

    returned_names = []
    if note:
        # Persist the note on the loan request itself so it shows up in the slip
        # history and on the printed slip, not only on the check-in transaction.
        request_obj.notes = (request_obj.notes or "").strip()
        request_obj.notes = (request_obj.notes + "\n" if request_obj.notes else "") + note
        request_obj.save(update_fields=["notes"])
    for entry in item_entries:
        item = entry["item"]
        quantity = max(1, int(entry.get("quantity", 1)))
        # Find this request's outstanding loan line for the item.
        line = request_obj.items.select_related("transaction", "item").filter(
            item=item
        ).first()
        source = line.transaction if line else None
        txn_notes = request_obj.notes or ""
        ok, error = record_item_transaction(
            item,
            "check_in",
            quantity,
            user=request_obj.user,
            location=line.location if line else None,
            notes=txn_notes,
            decided_by=decided_by,
            source_transaction=source,
        )
        if not ok:
            return False, error
        returned_names.append(f"{quantity} x {item.name}")

    # Mark the loan fully returned once every one of its check-out lines has a
    # matching approved check-in. This covers both book-ahead reservations (whose
    # hand-over spawned the check-out) and direct check-outs, so the slip status
    # reads "Returned" for either kind. `_close_reservations_on_return` already
    # handles the book-ahead case during the transaction record; this is the
    # authoritative check for the loan that owns this slip code.
    fully_returned = True
    for line in request_obj.items.select_related("transaction").all():
        checkout = line.transaction
        if not checkout:
            fully_returned = False
            break
        returned_qty = Transaction.objects.filter(
            source_transaction=checkout,
            transaction_type="check_in",
            approval_status="approved",
            voided="none",
        ).aggregate(total=Sum("quantity"))["total"] or 0
        if returned_qty < line.quantity:
            fully_returned = False
            break

    if fully_returned:
        request_obj.returned_at = timezone.now()
        request_obj.returned_by = decided_by
        request_obj.returned_by_name = display_name(decided_by) if decided_by else ""
        request_obj.save(
            update_fields=["returned_at", "returned_by", "returned_by_name"]
        )
    status = "Returned" if fully_returned else "Collected (partial)"
    return (
        True,
        f"Returned {', '.join(returned_names)} for {request_obj.reference_code} "
        f"— {status}.",
    )


@db_transaction.atomic
def _reject_request_obj(request_obj, decided_by):
    """Reject a multi-item Request. No stock is moved."""
    if request_obj.approval_status != "pending":
        return False, "This request has already been decided."
    request_obj.approval_status = "rejected"
    request_obj.decided_by = decided_by
    request_obj.decided_by_name = display_name(decided_by)
    request_obj.decided_at = timezone.now()
    request_obj.save(
        update_fields=["approval_status", "decided_by", "decided_by_name", "decided_at"]
    )
    notify_transaction_change(request_obj, "rejected", actor_name=display_name(decided_by))
    return True, None


@db_transaction.atomic
def void_request(request_obj, decided_by, *, reason=""):
    """Void an entire Request (and every child Transaction). Stock is reversed
    for any movement that already happened."""
    if request_obj.is_voided:
        return False, "This request has already been voided."
    for line in request_obj.items.select_related("transaction", "item").all():
        txn = line.transaction
        if txn and not txn.is_voided:
            ok, error = void_transaction(txn, decided_by, reason=reason)
            if not ok:
                return False, error
    request_obj.voided = "voided"
    request_obj.voided_by = decided_by
    request_obj.voided_by_name = display_name(decided_by)
    request_obj.voided_at = timezone.now()
    request_obj.voided_reason = reason or ""
    request_obj.save(
        update_fields=["voided", "voided_by", "voided_by_name", "voided_at", "voided_reason"]
    )
    notify_transaction_change(request_obj, "voided", actor_name=display_name(decided_by))
    return True, None


def _localize(dt):
    """Treat naive datetimes (from datetime-local form inputs) as South African
    local time and return them timezone-aware, so they store as the correct UTC
    value. Plain dates (from date inputs) are treated as local midnight. Aware
    datetimes are passed through unchanged."""
    if dt is None:
        return None
    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime(dt.year, dt.month, dt.day)
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _summarize_request(obj, now):
    """Build a display dict for a single request/transaction (pending, approved
    or rejected) for account and admin pages.

    Accepts either a :class:`Request` (multi-item) or a legacy single-item
    :class:`Transaction`; it reads the shared fields that both expose.
    """
    from .models import _summarize_request_items

    expected_return = obj.expected_return
    if expected_return:
        time_left = (
            f"Overdue by {timesince(expected_return, now)}"
            if expected_return < now
            else f"Due in {timeuntil(expected_return, now)}"
        )
    else:
        time_left = None
    # A Request's "item" is its first line; a Transaction has .item directly.
    item = getattr(obj, "item", None)
    if item is None:
        first_line = obj.items.first() if hasattr(obj, "items") else None
        item = first_line.item if first_line else None
    return {
        "req": obj,
        "item": item,
        "quantity": getattr(obj, "total_quantity", None) or getattr(obj, "quantity", 1),
        "transaction_type": obj.transaction_type,
        "type_label": obj.transaction_type.replace("_", " ").title(),
        "approval_status": obj.approval_status,
        "expected_return": expected_return,
        "taken_at": obj.taken_at,
        "is_overdue": bool(expected_return and expected_return < now),
        "time_left": time_left,
        "created_at": obj.created_at,
        "decided_by": obj.decided_by,
        "summary": _summarize_request_items(obj) if hasattr(obj, "items") else f"{obj.quantity} x {item.name if item else 'item'}",
    }


def get_user_borrowed_items(user):
    """Gear a user currently has out, grouped by item.

    Each item the user has out becomes one card. Within it, every still-outstanding
    check-out is a collapsible detail row, so distinct deadlines for the same item
    (e.g. two borrowed, one overdue and one due later) stay separate but don't
    each take a full row of vertical space.

    Returns (approved check-ins) consume the oldest check-outs first (FIFO), so
    the live check-outs are the most recent ones once the returned quantity is
    accounted for. Pending requests and approved book-aheads (reservations) are
    excluded here; see get_user_gear_summary() for those.

    The live loans are taken from get_user_active_loans(), which computes
    outstanding stock from the real transactions, so this panel and the
    check-in "which loan?" dropdown always agree on what is actually out.
    """
    now = timezone.now()

    def describe(expected_return):
        if not expected_return:
            return None, False
        time_left = (
            f"Overdue by {timesince(expected_return, now)}"
            if expected_return < now
            else f"Due in {timeuntil(expected_return, now)}"
        )
        return time_left, expected_return < now

    per_item = {}
    for loan in get_user_active_loans(user):
        for line in loan["items"]:
            item = line["item"]
            entry = per_item.setdefault(item, {"out": 0, "details": []})
            # The loan line's own checkout gives the true per-loan deadline.
            checkout = loan["loan"].items.filter(item=item).first()
            txn = checkout.transaction if checkout else None
            expected_return = txn.expected_return if txn else None
            time_left, overdue = describe(expected_return)
            entry["out"] += line["quantity"]
            entry["details"].append(
                {
                    "txn": txn,
                    "loan": loan["loan"],
                    "reference_code": loan["reference_code"],
                    "quantity": line["quantity"],
                    "expected_return": expected_return,
                    "is_overdue": overdue,
                    "time_left": time_left,
                }
            )

    grouped = []
    for item, data in per_item.items():
        details = data["details"]
        if not details:
            continue
        # Sort details soonest-deadline first so the most urgent is on top.
        details.sort(
            key=lambda d: (d["expected_return"] is None, d["expected_return"] or now)
        )
        overdue_count = sum(1 for d in details if d["is_overdue"])
        if overdue_count and overdue_count == len(details):
            summary = f"All {len(details)} overdue"
        elif overdue_count:
            summary = f"{overdue_count} overdue, {len(details) - overdue_count} due"
        else:
            summary = f"{len(details)} due"
        grouped.append(
            {
                "item": item,
                "quantity": data["out"],
                "details": details,
                "is_overdue": overdue_count > 0,
                "summary": summary,
                "overdue_count": overdue_count,
            }
        )

    grouped.sort(
        key=lambda entry: (entry["details"][0]["expected_return"] is None, entry["details"][0]["expected_return"] or now)
    )
    return grouped


def get_user_active_loans(user):
    """The user's live loans eligible to be returned, keyed by slip code.

    Returns approved, handed-over check-outs and book-aheads that are not yet
    fully returned and not voided. Each entry carries the loan's
    ``reference_code`` so the member can see (and quote) it, the items still
    out, and how many units remain to be brought back. This is what the Home
    "Info & updates" panel shows and what the check-in form lets the member
    pick from, so a return is filed against the exact loan instead of guessed.
    """
    from .models import Request, RequestItem, Transaction

    # A check-out is live the moment it is approved (stock moves at approval);
    # a book-ahead only becomes a live loan once it has been handed over at
    # pickup. Both count as active until they are fully returned or voided.
    loans = (
        Request.objects.filter(user=user, voided="none", approval_status="approved", returned_at__isnull=True, source_request__isnull=True)
        .filter(
            models.Q(transaction_type="check_out")
            | models.Q(transaction_type="book_ahead", handed_over_at__isnull=False)
        )
        .prefetch_related("items__item", "items__item__location")
        .order_by("-created_at")
    )

    def _checkout_for_line(line):
        """The approved check-out Transaction this loan line spawned.

        Only modern loans whose RequestItem actually links to its spawned
        checkout are counted as active. Legacy loans (no transaction link) are
        skipped: their units are already represented by the modern loan that
        owns the real checkout, and guessing the link would double-count the
        same physical unit across several legacy rows."""
        return line.transaction

    def _returned_for(checkout):
        if not checkout:
            return 0
        return (
            Transaction.objects.filter(
                source_transaction=checkout,
                transaction_type="check_in",
                approval_status="approved",
                voided="none",
            ).aggregate(total=models.Sum("quantity"))["total"]
            or 0
        )

    entries = []
    for loan in loans:
        loan_out = 0
        for line in loan.items.all():
            checkout = _checkout_for_line(line)
            if not checkout:
                # Legacy line with no spawned checkout: not attributable here.
                continue
            returned = _returned_for(checkout)
            outstanding = checkout.quantity - returned
            if outstanding > 0:
                loan_out += outstanding
        if loan_out <= 0:
            continue
        entries.append(
            {
                "loan": loan,
                "reference_code": loan.reference_code,
                "transaction_type": loan.transaction_type,
                "items": [
                    {"item": line.item, "quantity": line.quantity, "location": line.location}
                    for line in loan.items.all()
                ],
                "outstanding": loan_out,
            }
        )
    return entries


def get_user_gear_summary(user):
    """Everything a user needs to see about their own requests.

    Returns approved check-outs (borrowed), book-ahead reservations (any
    status) and still-pending check-out/in requests. These all live on the
    :class:`Request` model now; the underlying Transactions are only created
    once a request is approved.
    """
    from .models import Request, _summarize_request_items

    now = timezone.now()
    reservations = []
    pending = []
    for req in (
        Request.objects.filter(user=user, voided="none")
        .prefetch_related("items__item", "items__item__location", "decided_by")
        .order_by("-created_at")
    ):
        if req.transaction_type == "book_ahead":
            reservations.append(_summarize_request(req, now))
        elif req.approval_status == "pending" and req.transaction_type in {"check_out", "check_in"}:
            pending.append(_summarize_request(req, now))
    return {
        "reservations": reservations,
        "pending": pending,
    }


def get_user_request_history(user):
    """Unified, newest-first history of every request a member has raised
    (any type and status), for their account page. Returns and the loans they
    close share one slip code, so only one entry per code is shown — the loan
    itself, not its check-in. Each entry carries the desk slip code so the
    member can quote it when they return or collect gear."""
    from .models import Request

    now = timezone.now()
    seen_codes = set()
    entries = []
    for req in (
        Request.objects.filter(user=user, voided="none")
        .select_related("source_request")
        .prefetch_related("items__item", "items__item__location", "decided_by")
        .order_by("-created_at")
    ):
        code = req.reference_code
        if not code or code in seen_codes:
            continue
        # A return request points back to the loan it closes; keep the loan
        # (no source_request) as the single row for the shared slip code.
        if req.source_request_id is not None:
            continue
        seen_codes.add(code)
        entries.append(_summarize_request(req, now))
    return entries


@db_transaction.atomic
def set_item_maintenance(item, *, user=None, reason="", expected_return=None, quantity=1, location=None):
    """Move units of an item into maintenance and open a maintenance record."""
    item = Item.objects.select_for_update().get(pk=item.pk)
    quantity = max(1, int(quantity))
    quantity = min(quantity, item.quantity_available)
    item.quantity_maintenance = (item.quantity_maintenance or 0) + quantity
    item.status = _resolve_status(item)
    item.save(update_fields=["quantity_maintenance", "status"])
    return Maintenance.objects.create(
        item=item,
        location=location or item.location,
        quantity=quantity,
        reason=reason,
        reported_by=user,
        expected_return=_localize(expected_return),
    )


@db_transaction.atomic
def complete_item_maintenance(record, *, user=None, notes=""):
    """Close a maintenance record and return its units to available stock."""
    record = Maintenance.objects.select_for_update().get(pk=record.pk)
    record.completed_at = timezone.now()
    record.completed_by = user
    record.completed_by_name = display_name(user)
    record.outcome = "returned"
    if notes:
        record.notes = notes
    record.save(update_fields=["completed_at", "completed_by", "completed_by_name", "outcome", "notes"])

    item = Item.objects.select_for_update().get(pk=record.item.pk)
    returned = min(record.quantity or 0, item.quantity_maintenance or 0)
    item.quantity_maintenance = (item.quantity_maintenance or 0) - returned
    item.status = _resolve_status(item)
    item.save(update_fields=["quantity_maintenance", "status"])
    return record


@db_transaction.atomic
def write_off_maintenance(record, *, user=None, notes=""):
    """Close a maintenance record whose item could not be fixed.

    The units are removed from the system entirely (written off): they leave
    both the maintenance count and the item's total stock, so available stock
    is unaffected. Used when gear is broken beyond repair or lost in service.
    """
    record = Maintenance.objects.select_for_update().get(pk=record.pk)
    record.completed_at = timezone.now()
    record.completed_by = user
    record.completed_by_name = display_name(user)
    record.outcome = "written_off"
    if notes:
        record.notes = notes
    record.save(update_fields=["completed_at", "completed_by", "completed_by_name", "outcome", "notes"])

    item = Item.objects.select_for_update().get(pk=record.item.pk)
    lost = min(record.quantity or 0, item.quantity_maintenance or 0)
    item.quantity_maintenance = (item.quantity_maintenance or 0) - lost
    item.quantity_total = max(0, (item.quantity_total or 0) - lost)
    item.status = _resolve_status(item)
    item.save(update_fields=["quantity_maintenance", "quantity_total", "status"])
    return record
