from collections import defaultdict

from django.db import migrations


class DuplicateSkuError(Exception):
    """Raised to stop the migration when two items share a non-blank SKU.

    Deliberately NOT auto-resolved: an Item row represents stock at ONE
    location (Item.location is a single FK, not a per-location stock table),
    so two items sharing a SKU are not necessarily a data-entry mistake -
    they may be two real, separate stock pools (e.g. the same gear type kept
    in two different rooms) that were just typed in with the same code by
    accident. Auto-merging would silently move one location's stock into the
    other's; auto-renaming would silently invalidate an already-printed
    physical asset-tag sticker. Both are judgement calls only a human with
    the physical inventory in front of them should make.
    """


def check_no_duplicate_skus(apps, schema_editor):
    Item = apps.get_model("inventory", "Item")

    groups = defaultdict(list)
    for item in Item.objects.select_related("location").all():
        sku = (item.sku or "").strip()
        if sku:
            groups[sku.lower()].append(item)

    conflicts = {sku: items for sku, items in groups.items() if len(items) > 1}
    if not conflicts:
        return

    lines = ["Duplicate Item SKUs found - migration 0020 cannot add the "
             "uniqueness constraint until these are resolved by hand:"]
    for sku, items in conflicts.items():
        lines.append(f"\n  SKU '{sku}':")
        for item in items:
            location = item.location.name if item.location_id else "(no location)"
            lines.append(
                f"    - Item #{item.pk} '{item.name}' at '{location}', "
                f"quantity_total={item.quantity_total}"
            )
    lines.append(
        "\nFor each conflict, decide in the app itself:\n"
        "  - If these really are the SAME stock (rare, since each Item row "
        "is tied to one location): edit the newer item and give it a "
        "different SKU, or use the app's existing 'edit item, matching SKU "
        "merges into the target' behaviour deliberately.\n"
        "  - If these are genuinely separate stock in different locations "
        "(the common case): give each a distinct SKU and reprint any "
        "physical asset-tag label that already carries the old code.\n"
        "Then re-run `python manage.py migrate`."
    )
    raise DuplicateSkuError("\n".join(lines))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0018_maintenance_outcome"),
    ]

    operations = [
        migrations.RunPython(check_no_duplicate_skus, noop),
    ]
