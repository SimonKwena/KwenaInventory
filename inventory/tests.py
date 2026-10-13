from datetime import timedelta
from io import BytesIO

import openpyxl
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CatalogItem, ConditionOption, Location, Notification, StatusOption, StockEntry, Transaction
from .services import (
    apply_request,
    create_pending_request,
    create_request,
    get_user_borrowed_items,
    hand_over_request,
    record_item_transaction,
    reject_request,
    return_request_by_code,
    void_transaction,
)


class InventoryViewsTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Main Store")
        self.condition = ConditionOption.objects.create(name="Good")
        self.status = StatusOption.objects.create(name="Available")

    def test_home_page_renders_with_google_status_message(self):
        response = self.client.get(reverse("inventory:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue with Google")

    def test_home_page_requires_login_before_showing_booking_actions(self):
        response = self.client.get(reverse("inventory:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue as guest")
        self.assertNotContains(response, "Book ahead or check in/out")

    def test_item_detail_page_loads_for_existing_item(self):
        catalog = CatalogItem.objects.create(
            name="Torch",
            description="Portable torch",
            sku="TORCH-01",
        )
        item = StockEntry.objects.create(
            catalog_item=catalog,
            location=self.location,
            quantity_total=5,
            quantity_out=0,
            condition=self.condition,
            status=self.status,
        )
        response = self.client.get(reverse("inventory:item_detail", args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, item.name)

    def test_authenticated_home_page_offers_book_ahead_option(self):
        user = get_user_model().objects.create_user(username="tester", email="tester@kwenamusic.co.za", password="secret123")
        self.client.force_login(user)
        response = self.client.get(reverse("inventory:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Book ahead")

    def test_authenticated_non_workspace_user_sees_workspace_message(self):
        user = get_user_model().objects.create_user(username="tester", email="tester@example.com", password="secret123")
        self.client.force_login(user)
        response = self.client.get(reverse("inventory:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Workspace access needed")
        self.assertNotContains(response, "Save action")

class ApprovalFlowTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Main Store")
        self.condition = ConditionOption.objects.create(name="Good")
        self.status = StatusOption.objects.create(name="Available")
        self.out_status = StatusOption.objects.create(name="Out")
        catalog = CatalogItem.objects.create(
            name="Torch",
            sku="TORCH-01",
        )
        self.item = StockEntry.objects.create(
            catalog_item=catalog,
            location=self.location,
            quantity_total=5,
            quantity_out=0,
            condition=self.condition,
            status=self.status,
        )
        self.member = get_user_model().objects.create_user(
            username="member", email="member@kwenamusic.co.za", password="secret123"
        )
        self.admin = get_user_model().objects.create_superuser(
            username="admin", email="admin@kwenamusic.co.za", password="secret123"
        )

    def test_user_checkout_request_does_not_move_stock_until_approved(self):
        txn, error = create_pending_request(
            self.item, "check_out", 2, user=self.member, expected_return=self._future()
        )
        self.assertIsNone(error)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)
        self.assertEqual(txn.approval_status, "pending")

        ok, error = apply_request(txn, decided_by=self.admin)
        self.assertTrue(ok)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 2)
        self.assertEqual(txn.approval_status, "approved")

    def test_handover_then_checkin_marks_reservation_returned(self):
        req, error = create_request(
            self.member, "book_ahead",
            [{"item": self.item, "quantity": 1}],
            expected_return=self._future(), taken_at=self._future(),
        )
        self.assertIsNone(error)
        apply_request(req, decided_by=self.admin)
        ok, error = hand_over_request(req, decided_by=self.admin)
        self.assertTrue(ok)
        req.refresh_from_db()
        self.assertTrue(req.is_handed_over)
        self.assertIsNone(req.returned_at)

        # The member returns the gear (check-in), which should close the
        # reservation instead of leaving it stuck on "Collected".
        record_item_transaction(
            self.item, "check_in", 1, user=self.member, decided_by=self.admin
        )
        req.refresh_from_db()
        self.assertTrue(req.is_returned)
        self.assertIsNotNone(req.returned_at)

    def test_unrelated_checkin_does_not_close_book_ahead(self):
        """Jon checks in gear from a separate check-out of the same item; that
        must NOT mark an unrelated handed-over book-ahead as returned."""
        req, error = create_request(
            self.member, "book_ahead",
            [{"item": self.item, "quantity": 4}],
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(req, decided_by=self.admin)
        hand_over_request(req, decided_by=self.admin)

        # Separate check-out by a different member.
        other = get_user_model().objects.create_user(
            username="jon", email="jon@kwenamusic.co.za", password="x"
        )
        co, err = create_request(
            other, "check_out", [{"item": self.item, "quantity": 2}],
            expected_return=self._future(),
        )
        apply_request(co, decided_by=self.admin)
        # Jon returns 1 of his 2 checked-out units.
        record_item_transaction(self.item, "check_in", 1, user=other, decided_by=self.admin)

        req.refresh_from_db()
        self.assertFalse(req.is_returned)
        self.assertIsNone(req.returned_at)

    def test_legacy_handed_over_without_linked_checkout_still_closes(self):
        """A handed-over book-ahead whose RequestItem lost its spawned
        check-out link must still flip to Returned once its gear comes back,
        instead of being stuck on Collected."""
        from .models import RequestItem

        req, error = create_request(
            self.member, "book_ahead",
            [{"item": self.item, "quantity": 2}],
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(req, decided_by=self.admin)
        hand_over_request(req, decided_by=self.admin)
        # Simulate legacy data: the spawned check-out link is missing.
        req.items.update(transaction=None)
        req.refresh_from_db()
        self.assertTrue(req.is_handed_over)
        self.assertFalse(req.is_returned)

        for _ in range(2):
            record_item_transaction(self.item, "check_in", 1, user=self.member, decided_by=self.admin)
        req.refresh_from_db()
        self.assertTrue(req.is_returned)
        self.assertIsNotNone(req.returned_at)

    def test_return_request_by_code_links_precisely(self):
        """Returning via the slip code credits the exact loan (source_transaction)
        and flips the reservation to Returned."""
        req, error = create_request(
            self.member, "book_ahead",
            [{"item": self.item, "quantity": 3}],
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(req, decided_by=self.admin)
        hand_over_request(req, decided_by=self.admin)
        self.assertIsNotNone(req.reference_code)

        ok, msg = return_request_by_code(
            req.reference_code,
            [{"item": self.item, "quantity": 3}],
            decided_by=self.admin,
        )
        self.assertTrue(ok)
        req.refresh_from_db()
        self.assertTrue(req.is_returned)

        # The check-in must be linked to this reservation's spawned checkout.
        line = req.items.select_related("transaction").first()
        checkin = Transaction.objects.get(
            item=self.item, transaction_type="check_in", approval_status="approved"
        )
        self.assertEqual(checkin.source_transaction, line.transaction)

    def test_checkout_returned_via_child_request_marks_loan_returned(self):
        """A direct check-out returned through a child check-in request (not the
        return-by-code desk path) is still marked Returned on the loan, so its
        status is not stuck on Approved."""
        loan, error = create_request(
            self.member, "check_out",
            [{"item": self.item, "quantity": 2}],
            expected_return=self._future(),
        )
        apply_request(loan, decided_by=self.admin)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 2)
        self.assertFalse(loan.is_returned)

        ret, error = create_request(
            self.member, "check_in",
            [{"item": self.item, "quantity": 2}],
            source_request=loan,
        )
        apply_request(ret, decided_by=self.admin)

        loan.refresh_from_db()
        self.assertTrue(loan.is_returned)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)

    def test_desk_return_can_be_voided_individually(self):
        """A return filed via return-by-code (no child request) is voidable on
        its own from the slip page; voiding it reopens the loan and puts the
        units back out."""
        req, error = create_request(
            self.member, "check_out",
            [{"item": self.item, "quantity": 2}],
            expected_return=self._future(),
        )
        apply_request(req, decided_by=self.admin)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 2)

        ok, msg = return_request_by_code(
            req.reference_code, [{"item": self.item, "quantity": 2}],
            decided_by=self.admin,
        )
        self.assertTrue(ok)
        req.refresh_from_db()
        self.assertTrue(req.is_returned)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)

        checkin = Transaction.objects.get(
            item=self.item, transaction_type="check_in", voided="none"
        )
        # The slip page exposes it as an individually voidable desk return.
        self.client.force_login(self.admin)
        resp = self.client.get(
            reverse("inventory:request_slip", args=[req.reference_code])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(checkin, resp.context["desk_returns"])

        # Void just that return.
        resp = self.client.post(
            reverse("inventory:transaction_void", args=[checkin.pk]),
            {"reason": "mis-scanned"},
        )
        self.assertEqual(resp.status_code, 302)
        checkin.refresh_from_db()
        self.assertEqual(checkin.voided, "voided")
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 2)  # units are back out
        req.refresh_from_db()
        self.assertIsNone(req.returned_at)  # loan reopened
        self.assertFalse(req.is_returned)

    def test_return_request_by_code_rejects_unknown(self):
        ok, msg = return_request_by_code("KW-9999", [], decided_by=self.admin)
        self.assertFalse(ok)
        self.assertIn("No request", msg)
        txn, error = create_pending_request(
            self.item, "book_ahead", 1, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(txn, decided_by=self.admin)
        self.item.refresh_from_db()
        # Approving a book-ahead is a reservation: stock is untouched.
        self.assertEqual(self.item.quantity_out, 0)
        self.assertEqual(self.item.quantity_available, 5)
        txn, error = create_pending_request(
            self.item, "book_ahead", 2, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(txn, decided_by=self.admin)
        note = Notification.objects.filter(user=self.member).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.category, "reservation")
        self.assertEqual(note.title, "Reservation approved")
        self.assertFalse(note.is_read)
        self.assertIn(self.item.name, note.message)

    def test_rejecting_book_ahead_notifies_member(self):
        txn, error = create_pending_request(
            self.item, "book_ahead", 1, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        reject_request(txn, decided_by=self.admin)
        note = Notification.objects.filter(user=self.member).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.title, "Reservation not approved")

    def test_non_book_ahead_requests_also_notify(self):
        txn, error = create_pending_request(
            self.item, "check_out", 2, user=self.member, expected_return=self._future()
        )
        apply_request(txn, decided_by=self.admin)
        note = Notification.objects.filter(user=self.member).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.category, "request")
        self.assertEqual(note.title, "Request approved")

    def test_editing_approved_request_without_changes_does_not_notify(self):
        self.client.force_login(self.admin)
        txn, error = create_pending_request(
            self.item, "check_out", 1, user=self.member, expected_return=self._future()
        )
        apply_request(txn, decided_by=self.admin)
        Notification.objects.filter(user=self.member).delete()
        response = self.client.post(
            reverse("inventory:request_edit", args=[txn.pk]),
            {
                "transaction_type": "check_out",
                "approval_status": "approved",
                "item": self.item.pk,
                "quantity": 1,
                "location": "",
                "condition": "",
                "expected_return": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
                "taken_at": "",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        # A no-op re-save should not create a duplicate "updated" notification.
        self.assertEqual(Notification.objects.filter(user=self.member).count(), 0)

    def test_editing_approved_book_ahead_notifies_member(self):
        self.client.force_login(self.admin)
        txn, error = create_pending_request(
            self.item, "book_ahead", 1, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(txn, decided_by=self.admin)
        Notification.objects.filter(user=self.member).delete()
        response = self.client.post(
            reverse("inventory:request_edit", args=[txn.pk]),
            {
                "transaction_type": "book_ahead",
                "approval_status": "approved",
                "item": self.item.pk,
                "quantity": 3,
                "location": "",
                "condition": "",
                "expected_return": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
                "taken_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "notes": "pickup changed",
            },
        )
        self.assertEqual(response.status_code, 302)
        note = Notification.objects.filter(user=self.member).first()
        self.assertIsNotNone(note)
        self.assertEqual(note.title, "Reservation updated")

    def test_process_transaction_captures_per_item_location_and_condition(self):
        self.client.force_login(self.member)
        other_loc = Location.objects.create(name="Studio")
        response = self.client.post(
            reverse("inventory:process_transaction"),
            {
                "transaction_type": "book_ahead",
                "item_ids": [str(self.item.pk)],
                "quantities": ["1"],
                "location_ids": [str(other_loc.pk)],
                "condition_ids": [str(self.condition.pk)],
                "expected_return": (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
                "taken_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        from .models import Request

        req = Request.objects.filter(user=self.member, transaction_type="book_ahead").first()
        self.assertIsNotNone(req)
        line = req.items.first()
        self.assertEqual(line.location_id, other_loc.pk)
        # Book-ahead gear is assumed working, so condition is not captured.
        self.assertIsNone(line.condition_id)

    def test_member_sees_book_ahead_notification_on_account_page(self):
        txn, error = create_pending_request(
            self.item, "book_ahead", 1, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(txn, decided_by=self.admin)
        self.client.force_login(self.member)
        response = self.client.get(reverse("inventory:account"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reservation approved")
        self.assertContains(response, "Mark all as read")

    def test_mark_notifications_read_clears_unread(self):
        txn, error = create_pending_request(
            self.item, "book_ahead", 1, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(txn, decided_by=self.admin)
        self.client.force_login(self.member)
        response = self.client.post(reverse("inventory:notifications_mark_read"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Notification.objects.filter(user=self.member, is_read=False).count(), 0)

    def test_admin_can_edit_pending_request(self):
        self.client.force_login(self.admin)
        txn, error = create_pending_request(
            self.item, "check_out", 1, user=self.member, expected_return=self._future()
        )
        other = StockEntry.objects.create(
            catalog_item=CatalogItem.objects.create(name="Mic", sku="MIC-01"),
            location=self.location, quantity_total=3, quantity_out=0,
            condition=self.condition, status=self.status,
        )
        future_out = (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        future_in = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("inventory:request_edit", args=[txn.pk]),
            {
                "transaction_type": "book_ahead",
                "approval_status": "pending",
                "item": other.pk,
                "quantity": 2,
                "location": "",
                "condition": "",
                "expected_return": future_out,
                "taken_at": future_in,
                "notes": "edited by admin",
            },
        )
        self.assertEqual(response.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.transaction_type, "book_ahead")
        self.assertEqual(txn.item_id, other.pk)
        self.assertEqual(txn.quantity, 2)
        self.assertEqual(txn.notes, "edited by admin")
        # Editing never moves stock and keeps the request pending.
        self.assertEqual(txn.approval_status, "pending")
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)

    def test_admin_can_edit_approved_book_ahead(self):
        self.client.force_login(self.admin)
        txn, error = create_pending_request(
            self.item, "book_ahead", 1, user=self.member,
            expected_return=self._future(), taken_at=self._future(),
        )
        apply_request(txn, decided_by=self.admin)
        self.assertEqual(txn.approval_status, "approved")
        response = self.client.post(
            reverse("inventory:request_edit", args=[txn.pk]),
            {
                "transaction_type": "book_ahead",
                "approval_status": "approved",
                "item": self.item.pk,
                "quantity": 3,
                "location": "",
                "condition": "",
                "expected_return": (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
                "taken_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "notes": "pickup changed",
            },
        )
        self.assertEqual(response.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.quantity, 3)
        self.assertEqual(txn.notes, "pickup changed")
        # Still a reservation: no stock moved.
        self.assertEqual(txn.approval_status, "approved")
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)

    def _future(self):
        from django.utils import timezone

        return timezone.now() + timezone.timedelta(days=1)


class VoidTransactionTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Main Store")
        self.condition = ConditionOption.objects.create(name="Good")
        self.status = StatusOption.objects.create(name="Available")
        catalog = CatalogItem.objects.create(
            name="Torch",
            sku="TORCH-01",
        )
        self.item = StockEntry.objects.create(
            catalog_item=catalog,
            location=self.location,
            quantity_total=5,
            quantity_out=0,
            condition=self.condition,
            status=self.status,
        )
        self.member = get_user_model().objects.create_user(
            username="member", email="member@kwenamusic.co.za", password="secret123"
        )
        self.admin = get_user_model().objects.create_superuser(
            username="admin", email="admin@kwenamusic.co.za", password="secret123"
        )

    def test_voiding_approved_checkout_reverses_stock_and_notifies(self):
        txn, error = create_pending_request(
            self.item, "check_out", 2, user=self.member, expected_return=self._future()
        )
        apply_request(txn, decided_by=self.admin)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 2)

        ok, err = void_transaction(txn, self.admin, reason="wrong entry")
        self.assertTrue(ok)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)
        txn.refresh_from_db()
        self.assertEqual(txn.voided, "voided")
        self.assertEqual(txn.voided_by, self.admin)
        self.assertTrue(
            Notification.objects.filter(user=self.member, category="request").exists()
        )

    def test_voiding_pending_request_only_marks_voided(self):
        txn, error = create_pending_request(
            self.item, "check_out", 1, user=self.member, expected_return=self._future()
        )
        ok, err = void_transaction(txn, self.admin)
        self.assertTrue(ok)
        self.item.refresh_from_db()
        self.assertEqual(self.item.quantity_out, 0)
        txn.refresh_from_db()
        self.assertEqual(txn.voided, "voided")

    def test_voided_transaction_excluded_from_borrowed_list(self):
        txn, error = create_pending_request(
            self.item, "check_out", 3, user=self.member, expected_return=self._future()
        )
        apply_request(txn, decided_by=self.admin)
        void_transaction(txn, self.admin)
        self.assertEqual(get_user_borrowed_items(self.member), [])

    def _future(self):
        from django.utils import timezone

        return timezone.now() + timezone.timedelta(days=1)


IMPORT_HEADERS = [
    "Name", "SKU", "Description", "Category", "Subcategory", "Location",
    "Quantity", "Quantity Out", "Quantity Maintenance", "Condition", "Status",
]
IMPORT_FIELDS = (
    "name", "sku", "description", "category", "subcategory", "location",
    "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status",
)
IMPORT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _workbook_bytes(rows, headers=None):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Items"
    sheet.append(headers or IMPORT_HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class ItemImportTests(TestCase):
    def setUp(self):
        self.room_one = Location.objects.create(name="Gear Room")
        self.room_two = Location.objects.create(name="Stage")
        self.condition = ConditionOption.objects.create(name="Working")
        self.status = StatusOption.objects.create(name="Available")
        self.admin = get_user_model().objects.create_superuser(
            username="boss", email="boss@kwenamusic.co.za", password="secret123"
        )
        self.member = get_user_model().objects.create_user(
            username="member", email="member@kwenamusic.co.za", password="secret123"
        )

    def _row(self, name, sku="", description="", category="", subcategory="", location="",
             quantity_total=0, quantity_out=0, quantity_maintenance=0, condition="", status=""):
        return {
            "name": name, "sku": sku, "description": description, "category": category,
            "subcategory": subcategory, "location": location, "quantity_total": quantity_total,
            "quantity_out": quantity_out, "quantity_maintenance": quantity_maintenance,
            "condition": condition, "status": status,
        }

    def _sheet(self, rows, headers=None, name="sheet.xlsx"):
        sheet_rows = [[row.get(field, "") for field in IMPORT_FIELDS] for row in rows]
        return SimpleUploadedFile(name, _workbook_bytes(sheet_rows, headers), content_type=IMPORT_CONTENT_TYPE)

    def _upload(self, sheet, **extra):
        data = {"action": "preview", "sheet": sheet}
        data.update(extra)
        return self.client.post(reverse("inventory:item_import"), data)

    def _confirm(self, rows, **extra):
        """Submit the editable preview exactly as the browser would: one
        ``row-<n>-<field>`` input per row, so the values posted here are the
        (possibly edited) ones the view imports."""
        data = {"action": "confirm", "merge_existing": "1"}
        for index, row in enumerate(rows):
            for field, value in row.items():
                data[f"row-{index}-{field}"] = value
        data.update(extra)
        return self.client.post(reverse("inventory:item_import"), data)

    def test_preview_then_submit_creates_items_and_stock(self):
        rows = [
            self._row("XLR Cable 10m", "XLR-10M", "Balanced audio cable", "Cables", "XLR", "Gear Room", 5, condition="Working", status="Available"),
            self._row("XLR Cable 10m", "XLR-10M", "Balanced audio cable", "Cables", "XLR", "Stage", 2, condition="Working", status="Available"),
            self._row("Dynamic Microphone", description="Vocal microphone", category="Mics", subcategory="Dynamic", location="Gear Room", quantity_total=3, condition="Working", status="Available"),
        ]
        self.client.force_login(self.admin)
        response = self._upload(self._sheet(rows))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["new"], 3)
        self.assertEqual(response.context["summary"]["errors"], 0)
        # The preview is editable: every cell comes back as an input.
        self.assertContains(response, 'name="row-0-name"')
        self.assertContains(response, 'name="row-0-quantity_total"')
        # Nothing is written until the preview is submitted.
        self.assertEqual(CatalogItem.objects.count(), 0)

        response = self._confirm(rows)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import complete")
        self.assertEqual(CatalogItem.objects.count(), 2)
        xlr = CatalogItem.objects.get(sku="XLR-10M")
        mic = CatalogItem.objects.get(sku="dynamic-microphone")
        self.assertEqual(mic.name, "Dynamic Microphone")
        self.assertEqual(StockEntry.objects.filter(catalog_item=xlr).count(), 2)
        self.assertEqual(StockEntry.objects.get(catalog_item=xlr, location=self.room_one).quantity_total, 5)
        self.assertEqual(StockEntry.objects.get(catalog_item=xlr, location=self.room_two).quantity_total, 2)
        mic_stock = StockEntry.objects.get(catalog_item=mic)
        self.assertEqual(mic_stock.quantity_total, 3)
        self.assertEqual(mic_stock.condition, self.condition)
        self.assertEqual(mic_stock.status, self.status)
        # QR codes are generated on save, the same as the manual add-item form.
        self.assertTrue(mic_stock.qr_code)

    def test_edits_made_in_the_preview_are_what_gets_imported(self):
        rows = [self._row("Mic", "MIC-1", location="Roof", quantity_total=2)]
        self.client.force_login(self.admin)
        response = self._upload(self._sheet(rows))
        self.assertEqual(response.context["summary"]["errors"], 1)

        # The admin fixes the room name and the SKU in the preview and submits.
        rows[0]["location"] = "Gear Room"
        rows[0]["sku"] = "MIC-01"
        rows[0]["quantity_total"] = 7
        response = self._confirm(rows)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import complete")
        self.assertEqual(
            StockEntry.objects.get(catalog_item__sku="MIC-01", location=self.room_one).quantity_total, 7
        )

    def test_edit_that_introduces_an_error_reshows_the_preview(self):
        rows = [self._row("Mic", "MIC-1", location="Gear Room", quantity_total=2)]
        self.client.force_login(self.admin)
        self._upload(self._sheet(rows))

        rows[0]["location"] = "Nowhere"
        response = self._confirm(rows)
        self.assertEqual(response.status_code, 200)
        # The single quotes come back HTML-escaped, so match on the room name.
        self.assertContains(response, "Unknown location")
        self.assertContains(response, "Nowhere")
        self.assertEqual(CatalogItem.objects.count(), 0)

    def test_removed_row_is_skipped(self):
        rows = [
            self._row("Mic", "MIC-1", location="Gear Room", quantity_total=2),
            self._row("Cable", "CBL-1", location="Gear Room", quantity_total=4),
        ]
        self.client.force_login(self.admin)
        self._upload(self._sheet(rows))

        response = self._confirm(rows, **{"row-0-remove": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import complete")
        self.assertFalse(StockEntry.objects.filter(catalog_item__sku="MIC-1").exists())
        self.assertEqual(StockEntry.objects.get(catalog_item__sku="CBL-1").quantity_total, 4)

    def test_blank_row_added_to_the_preview_is_ignored(self):
        rows = [self._row("Mic", "MIC-1", location="Gear Room", quantity_total=2)]
        self.client.force_login(self.admin)
        self._upload(self._sheet(rows))

        rows.append({field: "" for field in IMPORT_FIELDS})
        response = self._confirm(rows)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import complete")
        self.assertEqual(CatalogItem.objects.count(), 1)

    def test_existing_sku_adds_stock_instead_of_duplicating(self):
        catalog = CatalogItem.objects.create(name="XLR Cable 10m", sku="XLR-10M", category="Cables")
        StockEntry.objects.create(catalog_item=catalog, location=self.room_one, quantity_total=5)
        self.client.force_login(self.admin)
        rows = [
            self._row("XLR Cable 10m", "XLR-10M", location="Gear Room", quantity_total=3),
            self._row("XLR Cable 10m", "XLR-10M", location="Stage", quantity_total=2),
        ]
        response = self._upload(self._sheet(rows))
        self.assertEqual(response.context["summary"]["existing"], 2)

        self._confirm(rows)
        self.assertEqual(CatalogItem.objects.count(), 1)
        self.assertEqual(StockEntry.objects.get(catalog_item=catalog, location=self.room_one).quantity_total, 8)
        self.assertEqual(StockEntry.objects.get(catalog_item=catalog, location=self.room_two).quantity_total, 2)

    def test_blank_sku_matches_existing_item_by_name(self):
        catalog = CatalogItem.objects.create(name="XLR Cable 10m", sku="XLR-10M")
        StockEntry.objects.create(catalog_item=catalog, location=self.room_one, quantity_total=4)
        self.client.force_login(self.admin)
        rows = [self._row("XLR Cable 10m", location="Gear Room", quantity_total=6)]
        self.assertEqual(self._upload(self._sheet(rows)).context["summary"]["existing"], 1)

        self._confirm(rows)
        self.assertEqual(CatalogItem.objects.count(), 1)
        self.assertEqual(StockEntry.objects.get(catalog_item=catalog, location=self.room_one).quantity_total, 10)

    def test_new_items_only_skips_existing_items(self):
        catalog = CatalogItem.objects.create(name="XLR Cable 10m", sku="XLR-10M")
        StockEntry.objects.create(catalog_item=catalog, location=self.room_one, quantity_total=5)
        self.client.force_login(self.admin)
        rows = [
            self._row("XLR Cable 10m", "XLR-10M", location="Gear Room", quantity_total=3),
            self._row("New Mic", "MIC-01", category="Mics", subcategory="Dynamic", location="Stage", quantity_total=2),
        ]
        response = self._upload(self._sheet(rows), merge_existing="0")
        self.assertFalse(response.context["merge_existing"])

        self._confirm(rows, merge_existing="0")
        self.assertEqual(CatalogItem.objects.count(), 2)
        self.assertEqual(StockEntry.objects.get(catalog_item=catalog, location=self.room_one).quantity_total, 5)
        self.assertEqual(StockEntry.objects.get(catalog_item__sku="MIC-01", location=self.room_two).quantity_total, 2)

    def test_missing_required_column_is_reported(self):
        self.client.force_login(self.admin)
        headers = IMPORT_HEADERS[:6] + IMPORT_HEADERS[7:]
        rows = [self._row("Mic", "MIC-1", location="Gear Room", quantity_total=2)]
        response = self._upload(self._sheet(rows, headers=headers))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CatalogItem.objects.count(), 0)

    def test_non_xlsx_upload_is_rejected(self):
        self.client.force_login(self.admin)
        upload = SimpleUploadedFile(
            "items.csv", b"Name,Location,Quantity\nMic,Gear Room,2\n", content_type="text/csv"
        )
        response = self._upload(upload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CatalogItem.objects.count(), 0)

    def test_duplicate_rows_for_same_room_are_added_together(self):
        self.client.force_login(self.admin)
        rows = [
            self._row("Mic", "MIC-1", category="Mics", location="Gear Room", quantity_total=2),
            self._row("Mic", "MIC-1", category="Mics", location="Gear Room", quantity_total=3),
        ]
        self._upload(self._sheet(rows))
        self._confirm(rows)
        self.assertEqual(StockEntry.objects.get(catalog_item__sku="MIC-1", location=self.room_one).quantity_total, 5)

    def test_blank_quantity_in_sheet_is_warned_and_saved_as_zero(self):
        self.client.force_login(self.admin)
        row = self._row("Mic", "MIC-1", location="Gear Room", quantity_total="")
        response = self._upload(self._sheet([row]))
        self.assertContains(response, "Quantity was blank")

        self._confirm([row])
        self.assertEqual(StockEntry.objects.get(catalog_item__sku="MIC-1", location=self.room_one).quantity_total, 0)

    def test_template_download_returns_a_valid_workbook(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("inventory:item_import_template"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], IMPORT_CONTENT_TYPE)
        workbook = openpyxl.load_workbook(BytesIO(response.content))
        sheet = workbook.worksheets[0]
        headers = [cell.value for cell in sheet[1]]
        self.assertEqual(headers, IMPORT_HEADERS)
        self.assertIn("Lists", workbook.sheetnames)
        self.assertIn("Gear Room", [row[0] for row in workbook["Lists"].iter_rows(values_only=True)])

    def test_import_pages_require_superadmin(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("inventory:item_import")).status_code, 302)
        self.assertEqual(self.client.get(reverse("inventory:item_import_template")).status_code, 302)
        rows = [self._row("Mic", "MIC-1", location="Gear Room", quantity_total=2)]
        self.assertEqual(self._upload(self._sheet(rows)).status_code, 302)
        self.assertEqual(self._confirm(rows).status_code, 302)
        self.assertEqual(CatalogItem.objects.count(), 0)
