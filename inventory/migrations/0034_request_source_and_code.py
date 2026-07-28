from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0033_request_reference_code'),
    ]

    operations = [
        migrations.AlterField(
            model_name='request',
            name='reference_code',
            field=models.CharField(
                blank=True,
                help_text='Short desk-facing code (e.g. KW-0042) printed on the slip so staff can link returns precisely. A return request reuses the code of the loan it closes.',
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='request',
            name='source_request',
            field=models.ForeignKey(
                blank=True,
                help_text='For a check-in request, the loan (check-out / book-ahead) it returns gear against, so it shares that loan\'s slip code.',
                null=True,
                on_delete=models.CASCADE,
                related_name='returns',
                to='inventory.request',
            ),
        ),
    ]
