from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0025_seed_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="request",
            name="handed_over_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="request",
            name="handed_over_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="handed_over_requests",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="request",
            name="handed_over_by_name",
            field=models.CharField(
                blank=True,
                help_text="Human-readable name of the admin who handed the gear over at pickup.",
                max_length=150,
            ),
        ),
    ]
