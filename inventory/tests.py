from datetime import timedelta

from django.contrib.auth import get_user_model
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
