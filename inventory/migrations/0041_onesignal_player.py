from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0040_add_reminder_category'),
    ]

    operations = [
        migrations.CreateModel(
            name='OneSignalPlayer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('player_id', models.CharField(max_length=255, unique=True)),
                ('user_agent', models.CharField(blank=True, max_length=512)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_used', models.DateTimeField(default=django.utils.timezone.now)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='onesignal_players', to='auth.user')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['user', 'last_used'], name='inventory_o_user_id_f9f1f9_idx')],
            },
        ),
        migrations.DeleteModel(
            name='WebPushDevice',
        ),
    ]
