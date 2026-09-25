from pathlib import Path

import openpyxl
from django.core.management.base import BaseCommand

from inventory.models import CatalogItem, ConditionOption, Location, StatusOption, StockEntry


def categorize_item(name):
    """Derive (category, subcategory, description) from an item name.

    The workbook's BY ITEM sheet has no type column, so we infer the group
    from the name. Cables are sub-grouped by their specific type (XLR,
    1/4 Jack, AUX, HDMI, Ethernet, Extension, ...).
    """
    n = (name or "").lower()
    if "xlr" in n:
        return ("Cables", "XLR", "Balanced XLR audio cable")
    if "jack" in n or "1/4" in n or "trs" in n:
        return ("Cables", "1/4 Jack", "Instrument / speaker cable")
    if "aux" in n:
        return ("Cables", "AUX", "AUX audio cable")
    if "hdmi" in n:
        return ("Cables", "HDMI", "HDMI video cable")
    if "ethernet" in n or "network" in n or "cat" in n:
        return ("Cables", "Ethernet", "Network / Ethernet cable")
    if "extension" in n:
        return ("Cables", "Extension", "Power extension cable")
    if "kettle" in n:
        return ("Power", "Kettle Plug", "Kettle / IEC power connector")
    if "multi-plug" in n or "multiplug" in n or "plug" in n:
        return ("Power", "Multi-plug", "Power multi-plug")
    if "microphone" in n or "mic" in n:
        sub = "Dynamic" if "dynamic" in n else "General"
        return ("Mics", sub, "Microphone")
    return ("Other", "General", "")


class Command(BaseCommand):
    help = "Seed the inventory app with workbook-style locations, items, conditions, and statuses"

    def handle(self, *args, **options):
        for name in ["Working", "Damaged", "Missing", "Needs Repair"]:
            ConditionOption.objects.get_or_create(name=name)

        for name in ["Available", "Out", "Maintenance", "Late Return"]:
            StatusOption.objects.get_or_create(name=name)

        gear_room, _ = Location.objects.get_or_create(name="Gear Room", defaults={"description": "Main storage area"})
        stage, _ = Location.objects.get_or_create(name="Stage", defaults={"description": "On stage / event support"})

        workbook_path = Path(__file__).resolve().parents[3] / "GEAR ROOM (Stock In_Out) (16).xlsx"
        if not workbook_path.exists():
            self.stdout.write(self.style.WARNING("Workbook not found; using fallback demo data."))
            fallback_items = [
                ("XLR - 30M", "Balanced audio cable", "XLR-30M", 5, 0, 0, gear_room, "Cables", "Audio"),
                ("XLR - 20M", "Balanced audio cable", "XLR-20M", 2, 0, 0, gear_room, "Cables", "Audio"),
                ("XLR - 10M", "Balanced audio cable", "XLR-10M", 7, 3, 4, gear_room, "Cables", "Audio"),
                ("XLR - 5M", "Balanced audio cable", "XLR-5M", 7, 0, 7, gear_room, "Cables", "Audio"),
                ("XLR - 3M", "Balanced audio cable", "XLR-3M", 3, 3, 0, gear_room, "Cables", "Audio"),
                ("1/4 Jack", "Instrument cable", "JACK-14", 14, 14, 0, gear_room, "Cables", "Instrument"),
                ("Dynamic Microphone", "Mic for live events", "MIC-DYN", 9, 9, 0, gear_room, "Mics", "Dynamic"),
                ("HDMI", "HDMI cable", "HDMI-001", 6, 6, 0, stage, "Cables", "Video"),
                ("Multi-plug", "Power adapter", "PLUG-001", 2, 2, 0, stage, "Power", "Accessories"),
                ("Extension Cable", "Power extension", "EXT-001", 2, 2, 0, stage, "Cables", "Power"),
                ("AUX", "AUX cable", "AUX-001", 2, 2, 0, stage, "Cables", "Audio"),
                ("Ethernet", "Network cable", "ETH-001", 4, 4, 0, stage, "Cables", "Network"),
                ("Kettle Plug", "Power connector", "KETTLE-001", 16, 16, 0, stage, "Power", "Accessories"),
                ("XLR - 15M", "Balanced audio cable", "XLR-15M", 5, 5, 0, stage, "Cables", "Audio"),
            ]
            for name, description, sku, total, available, out, location, category, subcategory in fallback_items:
                catalog, _ = CatalogItem.objects.update_or_create(
                    name=name,
                    defaults={
                        "description": description,
                        "sku": sku,
                        "category": category,
                        "subcategory": subcategory,
                        "is_active": True,
                    },
                )
                StockEntry.objects.update_or_create(
                    catalog_item=catalog,
                    location=location,
                    defaults={
                        "quantity_total": total,
                        "quantity_out": out,
                        "quantity_maintenance": 0,
                        "condition": ConditionOption.objects.filter(name="Working").first(),
                        "status": StatusOption.objects.filter(name="Available").first(),
                        "is_active": True,
                    },
                )
            self.stdout.write(self.style.SUCCESS("Seeded fallback workbook-style inventory data"))
            return

        workbook = openpyxl.load_workbook(workbook_path, data_only=True)
        ws = workbook["BY ITEM"]
        condition = ConditionOption.objects.filter(name="Working").first()
        status = StatusOption.objects.filter(name="Available").first()

        imported = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            name = (row[0] or "").strip() if len(row) > 0 else ""
            if not name or name.lower() in {"item name", "all items"}:
                continue
            if name.lower().startswith("https://"):
                continue
            total = row[1] if len(row) > 1 else None
            out = row[2] if len(row) > 2 else None
            available = row[3] if len(row) > 3 else None

            def parse_value(value):
                if value in (None, ""):
                    return 0
                if isinstance(value, (int, float)):
                    return int(value)
                try:
                    return int(float(str(value).replace(",", "")))
                except ValueError:
                    return 0

            category, subcategory, description = categorize_item(name)
            catalog, created = CatalogItem.objects.update_or_create(
                name=name,
                defaults={
                    "description": description or f"Imported from {workbook_path.name}",
                    "sku": name.lower().replace(" ", "-").replace("/", "-"),
                    "category": category,
                    "subcategory": subcategory,
                    "is_active": True,
                },
            )
            StockEntry.objects.update_or_create(
                catalog_item=catalog,
                location=gear_room,
                defaults={
                    "quantity_total": parse_value(total),
                    "quantity_out": parse_value(out),
                    "quantity_maintenance": 0,
                    "condition": condition,
                    "status": status,
                    "is_active": True,
                },
            )
            if created:
                imported += 1

        self.stdout.write(self.style.SUCCESS(f"Imported workbook items into inventory ({imported} new items)"))
