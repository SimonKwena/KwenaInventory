from django.db import migrations


def backfill_roles(apps, schema_editor):
    """Create a UserProfile for every user and set ``role`` from the existing
    is_superuser / is_staff flags and guest_profile presence."""
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("inventory", "UserProfile")
    GuestProfile = apps.get_model("inventory", "GuestProfile")

    guest_user_ids = set(GuestProfile.objects.values_list("user_id", flat=True))

    for user in User.objects.all():
        if user.is_superuser:
            role = "superadmin"
        elif user.is_staff:
            role = "staff"
        elif user.id in guest_user_ids:
            role = "guest"
        else:
            role = "student"
        UserProfile.objects.update_or_create(user=user, defaults={"role": role})


def revert_roles(apps, schema_editor):
    UserProfile = apps.get_model("inventory", "UserProfile")
    UserProfile.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0021_userprofile'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(backfill_roles, revert_roles),
    ]
