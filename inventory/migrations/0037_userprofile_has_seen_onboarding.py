from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0036_stocktake_stocktakeitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='has_seen_onboarding',
            field=models.BooleanField(default=False, help_text='Whether the user has dismissed the first-login onboarding popup.'),
        ),
    ]
