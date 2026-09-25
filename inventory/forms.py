from django import forms
from django.contrib.auth.models import User
from django.forms.models import BaseModelFormSet, construct_instance, modelformset_factory

from .models import (
    Announcement,
    CatalogItem,
    ConditionOption,
    GuestProfile,
    Location,
    Maintenance,
    StatusOption,
    StockEntry,
    Transaction,
    UserProfile,
)
from .permissions import ROLE_CHOICES, visible_locations


class StockEntryRowForm(forms.ModelForm):
    class Meta:
        model = StockEntry
        fields = ["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"]
        labels = {
            "quantity_total": "Qty",
            "quantity_out": "Out",
            "quantity_maintenance": "Maint",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)
        self.fields["condition"].queryset = ConditionOption.objects.filter(is_active=True).order_by("name")
        self.fields["status"].queryset = StatusOption.objects.filter(is_active=True).order_by("name")


class StockEntryFormSet(BaseModelFormSet):
    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["user"] = self.user
        return super()._construct_form(i, **kwargs)


StockEntryFormSet = modelformset_factory(
    StockEntry,
    form=StockEntryRowForm,
    formset=StockEntryFormSet,
    extra=1,
    can_delete=True,
    fields=["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"],
)


class LocalAccountForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]
        labels = {
            "first_name": "First name",
            "last_name": "Last name",
            "email": "Email",
        }


class GuestProfileForm(forms.ModelForm):
    class Meta:
        model = GuestProfile
        fields = ["full_name", "email", "phone", "organization", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "full_name": "Full name",
            "email": "Email",
            "phone": "Phone",
            "organization": "Organization",
            "notes": "Purpose / notes",
        }


class GuestLoginForm(forms.Form):
    name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(label="Email")
    phone = forms.CharField(max_length=30, label="Phone")
    organization = forms.CharField(max_length=150, required=False, label="Organization")


class TransactionForm(forms.Form):
    transaction_type = forms.ChoiceField(
        choices=[
            ("book_ahead", "Book ahead"),
            ("check_out", "Check out"),
            ("check_in", "Check in"),
        ],
        label="Action",
    )
    asset_tag = forms.CharField(max_length=100, required=False, label="Asset tag / SKU")
    location = forms.ModelChoiceField(queryset=Location.objects.none(), required=False, label="Pick-up / Return Location")
    quantities = forms.CharField(required=False, label="Quantity")
    condition = forms.ModelChoiceField(queryset=ConditionOption.objects.filter(is_active=True).order_by('name'), required=False, label="Condition")
    expected_return = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Expected return")
    taken_at = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Taking on (date & time)")
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False, label="Notes")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)

    def clean(self):
        cleaned_data = super().clean()
        asset_tag = cleaned_data.get("asset_tag")
        item_ids = self.data.getlist("item_ids") if hasattr(self.data, "getlist") else []
        if not asset_tag and not item_ids:
            raise forms.ValidationError("Choose an item or provide an asset tag / SKU.")
        transaction_type = cleaned_data.get("transaction_type")
        if transaction_type in ("check_out", "book_ahead") and not cleaned_data.get("expected_return"):
            self.add_error("expected_return", "Expected return is required for check-out and book-ahead.")
        if transaction_type == "book_ahead" and not cleaned_data.get("taken_at"):
            self.add_error("taken_at", "Pickup date and time is required for book-ahead.")
        return cleaned_data


class ScanItemForm(forms.Form):
    asset_tag = forms.CharField(max_length=100, required=False, label="Asset tag / SKU")
    bulk_asset_tags = forms.CharField(widget=forms.HiddenInput(), required=False)
    location = forms.ModelChoiceField(queryset=Location.objects.none(), required=False, label="Location")
    transaction_type = forms.ChoiceField(
        choices=[("check_out", "Check out"), ("check_in", "Check in")],
        label="Action",
    )
    condition = forms.ModelChoiceField(queryset=ConditionOption.objects.filter(is_active=True).order_by('name'), required=False, label="Condition")
    expected_return = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Expected return")
    taken_at = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), label="Taking on (date & time)")
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False, label="Notes")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)

    def clean(self):
        cleaned_data = super().clean()
        asset_tag = cleaned_data.get("asset_tag", "")
        bulk_asset_tags = cleaned_data.get("bulk_asset_tags", "")
        if not asset_tag and not bulk_asset_tags:
            raise forms.ValidationError("Enter an asset tag / SKU or scan one or more items.")
        if cleaned_data.get("transaction_type") == "check_out" and not cleaned_data.get("expected_return"):
            self.add_error("expected_return", "Expected return is required when checking out gear.")
        return cleaned_data


class OverrideForm(ScanItemForm):
    asset_tag = forms.CharField(max_length=100, label="Asset tag / SKU")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("bulk_asset_tags", None)


class ItemForm(forms.ModelForm):
    category_other = forms.CharField(required=False, label="Other category")
    subcategory_other = forms.CharField(required=False, label="Other subcategory")

    class Meta:
        model = CatalogItem
        fields = ["name", "description", "category", "subcategory", "sku", "image"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "sku": forms.TextInput(attrs={"placeholder": "Unique code, e.g. XLR-30M"}),
        }
        labels = {
            "name": "Item name",
            "sku": "SKU",
            "image": "Photo (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta.validate_unique = False
        categories = list(
            CatalogItem.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
        )
        subcategories = list(
            CatalogItem.objects.exclude(subcategory="").values_list("subcategory", flat=True).distinct().order_by("subcategory")
        )
        self.fields["category"].widget = forms.Select(
            choices=[("", "— select —")] + [(c, c) for c in categories] + [("Other", "Other…")]
        )
        self.fields["subcategory"].widget = forms.Select(
            choices=[("", "— select —")] + [(s, s) for s in subcategories] + [("Other", "Other…")]
        )

    def _post_clean(self):
        self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)
        try:
            self.instance.validate_unique(exclude=["sku"])
        except forms.ValidationError as e:
            allowed = []
            for error in e.error_dict.get("__all__", []):
                if "unique_nonblank_catalog_item_sku" not in str(error):
                    allowed.append(error)
            if allowed:
                self._update_errors(forms.ValidationError(allowed))

    def clean_sku(self):
        return (self.cleaned_data.get("sku") or "").strip()

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get("category")
        if category == "Other":
            other = (cleaned.get("category_other") or "").strip()
            if not other:
                self.add_error("category_other", "Please type the category.")
            else:
                cleaned["category"] = other
        elif category:
            cleaned["category"] = category.strip()

        subcategory = cleaned.get("subcategory")
        if subcategory == "Other":
            other = (cleaned.get("subcategory_other") or "").strip()
            if not other:
                self.add_error("subcategory_other", "Please type the subcategory.")
            else:
                cleaned["subcategory"] = other
        elif subcategory:
            cleaned["subcategory"] = subcategory.strip()
        return cleaned


class StockEntryRowForm(forms.ModelForm):
    class Meta:
        model = StockEntry
        fields = ["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"]
        labels = {
            "quantity_total": "Qty",
            "quantity_out": "Out",
            "quantity_maintenance": "Maint",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)
        self.fields["condition"].queryset = ConditionOption.objects.filter(is_active=True).order_by("name")
        self.fields["status"].queryset = StatusOption.objects.filter(is_active=True).order_by("name")


StockEntryFormSet = modelformset_factory(
    StockEntry,
    form=StockEntryRowForm,
    extra=1,
    can_delete=True,
    fields=["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"],
)


class CatalogItemBasicForm(forms.ModelForm):
    category_other = forms.CharField(required=False, label="Other category")
    subcategory_other = forms.CharField(required=False, label="Other subcategory")

    class Meta:
        model = CatalogItem
        fields = ["name", "description", "category", "subcategory", "sku", "image"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "sku": forms.TextInput(attrs={"placeholder": "Unique code, e.g. XLR-30M"}),
        }
        labels = {
            "name": "Item name",
            "sku": "SKU",
            "image": "Photo (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._meta.validate_unique = False
        categories = list(
            CatalogItem.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
        )
        subcategories = list(
            CatalogItem.objects.exclude(subcategory="").values_list("subcategory", flat=True).distinct().order_by("subcategory")
        )
        self.fields["category"].widget = forms.Select(
            choices=[("", "— select —")] + [(c, c) for c in categories] + [("Other", "Other…")]
        )
        self.fields["subcategory"].widget = forms.Select(
            choices=[("", "— select —")] + [(s, s) for s in subcategories] + [("Other", "Other…")]
        )

    def _post_clean(self):
        self.instance = construct_instance(self, self.instance, self._meta.fields, self._meta.exclude)
        try:
            self.instance.validate_unique(exclude=["sku"])
        except forms.ValidationError as e:
            allowed = []
            for error in e.error_dict.get("__all__", []):
                if "unique_nonblank_catalog_item_sku" not in str(error):
                    allowed.append(error)
            if allowed:
                self._update_errors(forms.ValidationError(allowed))

    def clean_sku(self):
        return (self.cleaned_data.get("sku") or "").strip()

    def clean(self):
        cleaned = super().clean()
        unique_error = self.errors.get("__all__", [])
        if any("unique_nonblank_catalog_item_sku" in str(e) for e in unique_error):
            self.errors.pop("__all__", None)
        category = cleaned.get("category")
        if category == "Other":
            other = (cleaned.get("category_other") or "").strip()
            if not other:
                self.add_error("category_other", "Please type the category.")
            else:
                cleaned["category"] = other
        elif category:
            cleaned["category"] = category.strip()

        subcategory = cleaned.get("subcategory")
        if subcategory == "Other":
            other = (cleaned.get("subcategory_other") or "").strip()
            if not other:
                self.add_error("subcategory_other", "Please type the subcategory.")
            else:
                cleaned["subcategory"] = other
        elif subcategory:
            cleaned["subcategory"] = subcategory.strip()
        return cleaned


class StockEntryRowForm(forms.ModelForm):
    class Meta:
        model = StockEntry
        fields = ["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"]
        labels = {
            "quantity_total": "Qty",
            "quantity_out": "Out",
            "quantity_maintenance": "Maint",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)
        self.fields["condition"].queryset = ConditionOption.objects.filter(is_active=True).order_by("name")
        self.fields["status"].queryset = StatusOption.objects.filter(is_active=True).order_by("name")


class _StockEntryFormSet(BaseModelFormSet):
    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["user"] = self.user
        return super()._construct_form(i, **kwargs)


StockEntryFormSet = modelformset_factory(
    StockEntry,
    form=StockEntryRowForm,
    formset=_StockEntryFormSet,
    extra=1,
    can_delete=True,
    fields=["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"],
)


class StockEntryBasicForm(forms.ModelForm):
    class Meta:
        model = StockEntry
        fields = ["location", "quantity_total", "quantity_out", "quantity_maintenance", "condition", "status"]
        labels = {
            "quantity_total": "Total quantity",
            "quantity_out": "Quantity currently out",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)
        self.fields["condition"].queryset = ConditionOption.objects.filter(is_active=True).order_by("name")
        self.fields["status"].queryset = StatusOption.objects.filter(is_active=True).order_by("name")


class UserAdminForm(forms.ModelForm):
    """Create / edit a Django auth user from the custom admin area."""

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        required=False,
        label="Password",
        help_text="Leave blank to keep the current password.",
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        required=False,
        label="Confirm password",
    )
    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        label="Role",
        help_text=(
            "Controls access. Superadmin has full control (incl. user "
            "management); admin and staff get staff pages; teacher, student "
            "and guest are members only."
        ),
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active", "is_staff", "is_superuser"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["password"].help_text = "Required when creating a new user."
        # Default the role field from the user's existing profile/flags.
        if self.instance.pk:
            profile = getattr(self.instance, "user_profile", None)
            if profile and profile.role:
                self.fields["role"].initial = profile.role
            elif self.instance.is_superuser:
                self.fields["role"].initial = "superadmin"
            elif self.instance.is_staff:
                self.fields["role"].initial = "staff"

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("confirm_password")
        if password and password != confirm:
            self.add_error("confirm_password", "Passwords do not match.")
        if not self.instance.pk and not password:
            self.add_error("password", "Password is required for new users.")
        return cleaned

    def save(self, commit=True):
        from .permissions import set_user_role

        user = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            user.set_password(password)
        if commit:
            # set_user_role persists the user, syncs is_staff/is_superuser from
            # the chosen role, and saves the UserProfile role.
            user = set_user_role(user, self.cleaned_data.get("role", "student"))
        return user


class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ["title", "message", "style", "pinned", "visible_until"]
        widgets = {
            "message": forms.Textarea(attrs={"rows": 4}),
            "visible_until": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }
        labels = {
            "title": "Title (optional)",
            "message": "Message",
            "style": "Style",
            "pinned": "Pin to top",
            "visible_until": "Hide after (optional)",
        }


class MaintenanceForm(forms.Form):
    asset_tag = forms.CharField(max_length=100, required=False, label="Asset tag / SKU")
    item = forms.ModelChoiceField(
        queryset=StockEntry.objects.filter(is_active=True).select_related("catalog_item").order_by("catalog_item__name"),
        required=True,
        label="Item",
    )
    location = forms.ModelChoiceField(
        queryset=Location.objects.filter(is_active=True).order_by("name"),
        required=True,
        label="Location (where it is for service)",
    )
    quantity = forms.IntegerField(min_value=1, initial=1, label="Quantity", required=True)
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        required=True,
        label="Issue / reason",
    )
    expected_return = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Expected back",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)

    def clean(self):
        cleaned = super().clean()
        asset_tag = (cleaned.get("asset_tag") or "").strip()
        if asset_tag:
            item = StockEntry.objects.filter(catalog_item__sku__iexact=asset_tag, is_active=True).first()
            if not item:
                self.add_error("asset_tag", "No item found with that SKU.")
            else:
                cleaned["item"] = item
        if not cleaned.get("item"):
            self.add_error("item", "Choose an item or scan its QR code.")
        if not cleaned.get("location") and cleaned.get("item"):
            cleaned["location"] = cleaned["item"].location
        return cleaned


class RequestEditForm(forms.Form):
    """Edit a request's details and its approval status before/after a decision."""

    transaction_type = forms.ChoiceField(choices=Transaction.TRANSACTION_TYPES, label="Action")
    approval_status = forms.ChoiceField(choices=Transaction.APPROVAL_CHOICES, label="Approval status")
    item = forms.ModelChoiceField(
        queryset=StockEntry.objects.filter(is_active=True).select_related("catalog_item").order_by("catalog_item__name"),
        label="Item",
    )
    quantity = forms.IntegerField(min_value=1, initial=1, label="Quantity")
    location = forms.ModelChoiceField(
        queryset=Location.objects.none(),
        required=False,
        label="Pick-up / Return location",
    )
    condition = forms.ModelChoiceField(
        queryset=ConditionOption.objects.filter(is_active=True).order_by("name"),
        required=False,
        label="Condition",
    )
    expected_return = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        label="Expected return",
    )
    taken_at = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        label="Taking on (date & time)",
    )
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False, label="Notes")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(user)

    def clean(self):
        cleaned = super().clean()
        transaction_type = cleaned.get("transaction_type")
        if transaction_type in ("check_out", "book_ahead") and not cleaned.get("expected_return"):
            self.add_error("expected_return", "Expected return is required for check-out and book-ahead.")
        if transaction_type == "book_ahead" and not cleaned.get("taken_at"):
            self.add_error("taken_at", "Pickup date and time is required for book-ahead.")
        return cleaned
