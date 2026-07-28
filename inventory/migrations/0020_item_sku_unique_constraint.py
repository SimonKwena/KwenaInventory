from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0019_merge_duplicate_item_skus"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="item",
            constraint=models.UniqueConstraint(
                condition=models.Q(("sku", ""), _negated=True),
                fields=("sku",),
                name="unique_nonblank_item_sku",
            ),
        ),
    ]
