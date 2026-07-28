from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0030_alter_announcement_style'),
    ]

    operations = [
        migrations.AddField(
            model_name='request',
            name='returned_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When the handed-over gear was checked back in (auto-set from a check-in).",
            ),
        ),
    ]
