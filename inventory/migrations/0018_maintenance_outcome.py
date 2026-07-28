from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0017_maintenance_location"),
    ]

    operations = [
        migrations.AddField(
            model_name="maintenance",
            name="outcome",
            field=models.CharField(
                blank=True,
                max_length=20,
                choices=[
                    ("returned", "Returned / fixed"),
                    ("written_off", "Could not be fixed"),
                ],
                help_text="How the maintenance ended once the item came back (blank while open).",
            ),
        ),
    ]
