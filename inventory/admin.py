from django.contrib import admin

from .models import (
    Announcement,
    ConditionOption,
    GuestProfile,
    Item,
    Location,
    LocationRole,
    Maintenance,
    Request,
    RequestItem,
    Role,
    StatusOption,
    Transaction,
)


@admin.register(ConditionOption)
class ConditionOptionAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)


@admin.register(StatusOption)
class StatusOptionAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)


class LocationRoleInline(admin.TabularInline):
    model = LocationRole
    extra = 1


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "is_active")
    search_fields = ("name",)
    inlines = [LocationRoleInline]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("slug", "label")
    search_fields = ("slug", "label")


@admin.register(LocationRole)
class LocationRoleAdmin(admin.ModelAdmin):
    list_display = ("location", "role")
    list_filter = ("role",)
    search_fields = ("location__name", "role__slug")


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "category", "subcategory", "location", "quantity_total", "quantity_available", "quantity_out", "is_active")
    list_filter = ("location", "category", "subcategory", "is_active")
    search_fields = ("name", "sku", "category", "subcategory")


@admin.register(GuestProfile)
class GuestProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "organization", "created_at")
    search_fields = ("full_name", "email", "organization")


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "is_active", "created_by", "created_by_name", "created_at", "visible_until")
    list_filter = ("is_active", "created_at")
    search_fields = ("title", "message", "created_by_name")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("item", "transaction_type", "quantity", "person_name", "user", "location", "approval_status", "voided", "decided_by_name", "expected_return", "created_at")
    list_filter = ("transaction_type", "approval_status", "voided", "created_at")
    search_fields = ("item__name", "user__username", "person_name", "notes")


class RequestItemInline(admin.TabularInline):
    model = RequestItem
    extra = 0
    autocomplete_fields = ("item",)
    readonly_fields = ("transaction",)


@admin.register(Request)
class RequestAdmin(admin.ModelAdmin):
    list_display = (
        "reference_code",
        "user",
        "person_name",
        "transaction_type",
        "approval_status",
        "voided",
        "item_count",
        "total_quantity",
        "is_handed_over",
        "is_returned",
        "created_at",
    )
    list_filter = ("transaction_type", "approval_status", "voided", "created_at")
    search_fields = (
        "reference_code",
        "user__username",
        "person_name",
        "notes",
        "items__item__name",
    )
    readonly_fields = (
        "reference_code",
        "created_at",
        "decided_at",
        "decided_by_name",
        "handed_over_at",
        "handed_over_by_name",
        "returned_at",
        "voided_at",
        "voided_by_name",
    )
    autocomplete_fields = ("user", "source_request")
    inlines = [RequestItemInline]



@admin.register(Maintenance)
class MaintenanceAdmin(admin.ModelAdmin):
    list_display = ("item", "started_at", "expected_return", "completed_at", "reported_by_name", "reported_by", "completed_by_name", "completed_by")
    list_filter = ("completed_at", "started_at")
    search_fields = ("item__name", "reason", "notes", "reported_by_name", "completed_by_name")
    autocomplete_fields = ("item",)


_SETTINGS_VIEW_URL = "/admin/settings-view/"
_MASTER_LOG_URL = "/admin/master-log/"
_USERS_VIEW_URL = "/admin/users/"


def _patch_admin_app_list():
    """Append a 'Site Settings' and 'Users' entry to the admin sidebar/index so
    the custom admin pages show up as their own tabs. The views themselves
    enforce access, so the sidebar entries are purely navigational. The User
    Management tab is only shown to superusers."""
    site = admin.site
    original = site.get_app_list

    def get_app_list(request, app_label=None):
        app_list = original(request, app_label)
        app_list.append(
            {
                "app_label": "site_settings",
                "name": "Site Settings",
                "app_url": _SETTINGS_VIEW_URL,
                "has_module_perms": True,
                "models": [
                    {
                        "name": "Django Settings",
                        "object_name": "djangosettings",
                        "admin_url": _SETTINGS_VIEW_URL,
                        "add_url": None,
                        "view_only": True,
                        "perms": {
                            "view": True,
                            "add": False,
                            "change": False,
                            "delete": False,
                        },
                    }
                ],
            }
        )
        if request.user.is_superuser:
            app_list.append(
                {
                    "app_label": "master_log",
                    "name": "Master Log",
                    "app_url": _MASTER_LOG_URL,
                    "has_module_perms": True,
                    "models": [
                        {
                            "name": "Activity",
                            "object_name": "masterlog",
                            "admin_url": _MASTER_LOG_URL,
                            "add_url": None,
                            "view_only": True,
                            "perms": {
                                "view": True,
                                "add": False,
                                "change": False,
                                "delete": False,
                            },
                        }
                    ],
                }
            )
            app_list.append(
                {
                    "app_label": "admin_users",
                    "name": "User Management",
                    "app_url": _USERS_VIEW_URL,
                    "has_module_perms": True,
                    "models": [
                        {
                            "name": "Users",
                            "object_name": "adminuser",
                            "admin_url": _USERS_VIEW_URL,
                            "add_url": "/admin/users/new/",
                            "view_only": True,
                            "perms": {
                                "view": True,
                                "add": True,
                                "change": True,
                                "delete": True,
                            },
                        }
                    ],
                }
            )
        return app_list

    site.get_app_list = get_app_list


_patch_admin_app_list()
