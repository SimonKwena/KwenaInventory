from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0031_request_returned_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='transaction',
            name='source_transaction',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='returns',
                to='inventory.transaction',
            ),
        ),
    ]
