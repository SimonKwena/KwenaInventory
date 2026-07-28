from django.db import migrations


ROLES = [
    ("superadmin", "Superadmin"),
    ("admin", "Admin"),
    ("staff", "Staff"),
    ("teacher", "Teacher"),
    ("student", "Student"),
    ("guest", "Guest"),
]


def create_roles(apps, schema_editor):
    Role = apps.get_model("inventory", "Role")
    for slug, label in ROLES:
        Role.objects.update_or_create(slug=slug, defaults={"label": label})


def remove_roles(apps, schema_editor):
    Role = apps.get_model("inventory", "Role")
    Role.objects.filter(slug__in=[slug for slug, _ in ROLES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0024_role_locationrole_location_roles'),
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
