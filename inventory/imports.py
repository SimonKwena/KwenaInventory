"""Excel import for catalog items and per-room stock.

The sheet is one worksheet with one row per item per location, e.g. a cable
that lives in three rooms is three rows. Nothing is written until the admin
confirms: :func:`parse_upload` + :func:`build_preview` turn the sheet into a
JSON-safe list of rows labelled new / existing / error, and
:func:`apply_import` commits them in a single transaction using the same merge
rules as the manual "Add item" form (:func:`inventory.views.item_create`).
"""

import logging
import re
from io import BytesIO
from itertools import zip_longest

import openpyxl
from django.db import transaction
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .models import CatalogItem, ConditionOption, Location, StatusOption, StockEntry

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SKU_LENGTH = 100

IMPORT_FILE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ImportValidationError(Exception):
    """Raised when the sheet as a whole cannot be used (bad file, missing
    required columns, no rows)."""


# Canonical field per accepted header. Keys are matched after lower-casing and
# stripping every character that is not a letter or digit, so "Item Name",
# "item_name" and "ITEMNAME" all resolve to the same column.
COLUMN_ALIASES = {
    "name": "name",
    "item": "name",
    "itemname": "name",
    "sku": "sku",
    "code": "sku",
    "description": "description",
    "details": "description",
    "category": "category",
    "type": "category",
    "subcategory": "subcategory",
    "location": "location",
    "room": "location",
    "quantity": "quantity_total",
    "quantitytotal": "quantity_total",
    "qty": "quantity_total",
    "qtytotal": "quantity_total",
    "total": "quantity_total",
    "quantityout": "quantity_out",
    "qtyout": "quantity_out",
    "out": "quantity_out",
    "quantitymaintenance": "quantity_maintenance",
    "quantitymaint": "quantity_maintenance",
    "qtymaint": "quantity_maintenance",
    "maint": "quantity_maintenance",
    "maintenance": "quantity_maintenance",
    "condition": "condition",
    "status": "status",
}

REQUIRED_COLUMNS = {"name": "Name", "location": "Location", "quantity_total": "Quantity"}

TEXT_FIELDS = ("name", "sku", "description", "category", "subcategory", "location", "condition", "status")
QUANTITY_FIELDS = ("quantity_total", "quantity_out", "quantity_maintenance")
QUANTITY_LABELS = {"quantity_total": "Quantity", "quantity_out": "Quantity Out", "quantity_maintenance": "Quantity Maintenance"}

def _normalise_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def generate_sku(name):
    """Slug used when the sheet leaves the SKU blank, so every item still gets
    a stable unique code (matching how the seed importer derives codes)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug[:MAX_SKU_LENGTH]


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _parse_quantity(value, label):
    """Return (quantity, warning). Blank or unreadable values become 0 with a
    warning instead of failing the whole row; blanks in the optional Out and
    Maintenance columns stay silent."""
    if value in (None, ""):
        if label == QUANTITY_LABELS["quantity_total"]:
            return 0, "Quantity was blank — saved as 0."
        return 0, None
    if isinstance(value, bool):
        return 0, f"{label} was not a number — saved as 0."
    if isinstance(value, (int, float)):
        number = int(value)
    else:
        try:
            number = int(float(str(value).strip().replace(",", "")))
        except ValueError:
            return 0, f"{label} was not a number — saved as 0."
    if number < 0:
        return 0, f"{label} cannot be negative — saved as 0."
    return number, None


def _empty_record(row_number):
    return {
        "row": row_number,
        "remove": False,
        "name": "",
        "sku": "",
        "sku_generated": False,
        "description": "",
        "category": "",
        "subcategory": "",
        "location": "",
        "location_id": None,
        "quantity_total": 0,
        "quantity_out": 0,
        "quantity_maintenance": 0,
        "condition": "",
        "condition_id": None,
        "status": "",
        "status_id": None,
        "catalog_id": None,
        "result": "new",
        "notes": [],
        "warnings": [],
        "errors": [],
    }


def parse_upload(upload):
    """Read the first worksheet into JSON-safe row dicts. No DB writes.

    Raises :class:`ImportValidationError` when the file itself is unusable.
    """
    filename = (upload.name or "").lower()
    if not filename.endswith(".xlsx"):
        raise ImportValidationError("Please upload an Excel workbook in .xlsx format.")
    if upload.size and upload.size > MAX_UPLOAD_BYTES:
        raise ImportValidationError("That sheet is larger than 5 MB. Split it into smaller files and import them one at a time.")

    try:
        workbook = openpyxl.load_workbook(BytesIO(upload.read()), data_only=True)
    except Exception as exc:
        raise ImportValidationError("That file could not be read as an Excel workbook. Save it as .xlsx and try again.") from exc

    worksheet = workbook.worksheets[0]
    header_row = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if not header_row:
        raise ImportValidationError("The sheet is empty. It needs a header row with at least Name, Location and Quantity.")

    mapping = {}
    for index, value in enumerate(header_row):
        key = COLUMN_ALIASES.get(_normalise_header(value))
        if key and key not in mapping.values():
            mapping[index] = key

    missing = [label for key, label in REQUIRED_COLUMNS.items() if key not in mapping.values()]
    if missing:
        raise ImportValidationError(
            "The sheet is missing required column(s): " + ", ".join(missing) + ". Download the template to see the expected layout."
        )

    rows = []
    for row_number, values in enumerate(worksheet.iter_rows(min_row=2, values_only=True), start=2):
        if not any(_cell_text(value) for value in values):
            continue
        record = _empty_record(row_number)
        for index, key in mapping.items():
            raw = values[index] if index < len(values) else None
            if key in TEXT_FIELDS:
                record[key] = _cell_text(raw)
                continue
            record[key], warning = _parse_quantity(raw, QUANTITY_LABELS[key])
            if warning:
                record["warnings"].append(warning)
        rows.append(record)

    if not rows:
        raise ImportValidationError("No item rows were found below the header row.")
    return rows


def rows_from_post(post):
    """Rebuild row dicts from the editable preview, which renders one set of
    ``row-<n>-<field>`` inputs per row plus a remove checkbox.

    Admins can fix typos, quantities or room names before anything is written,
    so the submitted values (not the original sheet) are what gets validated
    and imported. Rows left completely blank — e.g. added with "+ Add row" and
    never filled in — are dropped rather than reported as errors.
    """
    rows = []
    index = 0
    while any(key.startswith(f"row-{index}-") for key in post.keys()):
        prefix = f"row-{index}-"
        record = _empty_record(index + 1)
        record["remove"] = post.get(prefix + "remove") in ("1", "on", "true")
        for field in TEXT_FIELDS:
            record[field] = _cell_text(post.get(prefix + field))
        for field in QUANTITY_FIELDS:
            quantity, warning = _parse_quantity(post.get(prefix + field), QUANTITY_LABELS[field])
            record[field] = quantity
            if warning:
                record["warnings"].append(warning)
        filled = any(record[field] for field in TEXT_FIELDS) or any(
            record[field] for field in QUANTITY_FIELDS
        )
        if filled or record["remove"]:
            rows.append(record)
        index += 1
    return rows


def build_preview(rows, merge_existing=True):
    """Resolve every parsed row against the live catalog: which rows create
    new items, which add stock to existing items (matched by SKU, then by name
    for blank SKUs), and which have errors. Still no DB writes.

    ``merge_existing`` only changes the wording of the preview; it is applied
    again in :func:`apply_import`.
    """
    locations = {loc.name.strip().lower(): loc for loc in Location.objects.all()}
    conditions = {opt.name.strip().lower(): opt for opt in ConditionOption.objects.filter(is_active=True)}
    statuses = {opt.name.strip().lower(): opt for opt in StatusOption.objects.filter(is_active=True)}

    catalogs_by_sku = {}
    catalogs_by_name = {}
    for catalog in CatalogItem.objects.all():
        if catalog.sku:
            catalogs_by_sku.setdefault(catalog.sku.strip().lower(), catalog)
        if catalog.name:
            catalogs_by_name.setdefault(catalog.name.strip().lower(), catalog)

    stock_totals = {
        (catalog_id, location_id): quantity
        for catalog_id, location_id, quantity in StockEntry.objects.values_list(
            "catalog_item_id", "location_id", "quantity_total"
        )
    }

    entries = []
    group_catalogs = {}
    seen_pairs = set()
    for record in rows:
        entry = dict(record)
        entry["notes"] = list(record["notes"])
        entry["warnings"] = list(record["warnings"])
        entry["errors"] = list(record["errors"])

        name = record["name"].strip()
        entry["name"] = name
        if not name:
            entry["errors"].append("Item name is required.")

        location_name = record["location"].strip()
        entry["location"] = location_name
        if not location_name:
            entry["errors"].append("Location is required — this sheet uses one row per room.")
        else:
            location = locations.get(location_name.lower())
            if location is None:
                entry["errors"].append(
                    f"Unknown location '{location_name}'. Use a location name from the template's Lists sheet."
                )
            else:
                entry["location_id"] = location.pk
                entry["location"] = location.name

        for field, options in (("condition", conditions), ("status", statuses)):
            value = record[field].strip()
            entry[field] = value
            if not value:
                continue
            option = options.get(value.lower())
            if option is None:
                entry["errors"].append(
                    f"Unknown {field} '{value}'. Leave it blank or use a name from the template's Lists sheet."
                )
            else:
                entry[field] = option.name
                entry[f"{field}_id"] = option.pk

        sku = record["sku"].strip()
        entry["sku_generated"] = not sku
        if not sku:
            sku = generate_sku(name)
        entry["sku"] = sku[:MAX_SKU_LENGTH]

        group_key = ("sku", entry["sku"].lower())
        if group_key not in group_catalogs:
            catalog = catalogs_by_sku.get(entry["sku"].lower())
            if catalog is None:
                # Cross-reference fallback: a blank or renamed SKU still links
                # to the live item when the name matches exactly, so re-imports
                # extend existing items instead of duplicating them.
                catalog = catalogs_by_name.get(name.lower())
                if catalog is not None:
                    entry["notes"].append(f"Matched existing item '{catalog.name}' by name.")
            group_catalogs[group_key] = catalog

        catalog = group_catalogs[group_key]
        entry["catalog_id"] = catalog.pk if catalog else None
        if catalog is not None:
            entry["result"] = "existing"
            entry["notes"].append(f"Stock will be added to existing item '{catalog.name}'.")
            if not merge_existing:
                entry["notes"].append("'New items only' is on, so this row will be skipped.")
        else:
            entry["result"] = "new"
            if entry["sku_generated"]:
                entry["notes"].append(f"SKU '{entry['sku']}' was generated from the name.")

        if entry["remove"]:
            entry["result"] = "removed"
            entry["notes"].append("Removed — this row will not be imported.")
        elif entry["errors"]:
            entry["result"] = "error"

        identity = entry["catalog_id"] or group_key
        pair_key = (identity, entry["location_id"])
        if entry["location_id"] and pair_key in seen_pairs:
            entry["notes"].append("Another row in this sheet has the same item and room — the quantities will be added together.")
        seen_pairs.add(pair_key)

        existing_total = stock_totals.get((entry["catalog_id"], entry["location_id"])) if entry["catalog_id"] else None
        if existing_total is not None:
            if merge_existing:
                entry["notes"].append(f"This room already holds {existing_total}; it will become {existing_total + entry['quantity_total']}.")
            else:
                entry["notes"].append(f"This room already holds {existing_total}; it will be left unchanged.")

        entries.append(entry)
    return entries


def summarize_preview(entries):
    """Counts shown above the preview table and in the summary messages."""
    summary = {
        "total": len(entries),
        "new": 0,
        "existing": 0,
        "errors": 0,
        "removed": 0,
        "warnings": 0,
    }
    for entry in entries:
        if entry["result"] == "error":
            summary["errors"] += 1
        elif entry["result"] == "removed":
            summary["removed"] += 1
        elif entry["result"] == "existing":
            summary["existing"] += 1
        else:
            summary["new"] += 1
        summary["warnings"] += len(entry["warnings"]) + len(entry["notes"])
    return summary


def apply_import(entries, merge_existing=True):
    """Commit previewed rows in a single transaction. Returns a summary dict.

    Mirrors ``inventory.views.item_create``: a new catalog item is created per
    unique item, and each row creates a ``StockEntry`` for its room or adds the
    quantities to the existing one. Rows flagged with errors are skipped.
    """
    summary = {
        "items_created": 0,
        "stock_created": 0,
        "stock_updated": 0,
        "skipped_existing": 0,
        "skipped_errors": 0,
        "skipped_removed": 0,
        "items": [],
    }
    try:
        with transaction.atomic():
            for entry in entries:
                if entry["result"] in ("error", "removed"):
                    if entry["result"] == "removed":
                        summary["skipped_removed"] += 1
                    else:
                        summary["skipped_errors"] += 1
                    continue
                if entry["result"] == "existing" and not merge_existing:
                    summary["skipped_existing"] += 1
                    continue

                catalog = CatalogItem.objects.filter(pk=entry["catalog_id"]).first() if entry["catalog_id"] else None
                if catalog is None:
                    catalog = CatalogItem.objects.create(
                        name=entry["name"],
                        sku=entry["sku"],
                        description=entry["description"],
                        category=entry["category"],
                        subcategory=entry["subcategory"],
                        is_active=True,
                    )
                    summary["items_created"] += 1

                stock = StockEntry.objects.filter(catalog_item=catalog, location_id=entry["location_id"]).first()
                if stock is not None:
                    if not merge_existing:
                        summary["skipped_existing"] += 1
                        continue
                    stock.quantity_total += entry["quantity_total"]
                    stock.quantity_out += entry["quantity_out"]
                    stock.quantity_maintenance += entry["quantity_maintenance"]
                    if entry["condition_id"]:
                        stock.condition_id = entry["condition_id"]
                    if entry["status_id"]:
                        stock.status_id = entry["status_id"]
                    stock.save()
                    summary["stock_updated"] += 1
                else:
                    stock = StockEntry.objects.create(
                        catalog_item=catalog,
                        location_id=entry["location_id"],
                        quantity_total=entry["quantity_total"],
                        quantity_out=entry["quantity_out"],
                        quantity_maintenance=entry["quantity_maintenance"],
                        condition_id=entry["condition_id"],
                        status_id=entry["status_id"],
                    )
                    summary["stock_created"] += 1
                summary["items"].append((catalog, stock.pk))
    except Exception:
        logger.exception("Excel item import failed; all changes rolled back")
        raise
    return summary


def build_template_workbook():
    """The workbook offered for download: an Items sheet with the header row,
    example rows (one item across two rooms, plus a blank-SKU row) and a Lists
    sheet naming every valid location, condition and status."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Items"
    headers = [
        "Name", "SKU", "Description", "Category", "Subcategory", "Location",
        "Quantity", "Quantity Out", "Quantity Maintenance", "Condition", "Status",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    locations = list(Location.objects.values_list("name", flat=True))
    first_room = locations[0] if locations else "Gear Room"
    second_room = locations[1] if len(locations) > 1 else "Room 1"
    condition = ConditionOption.objects.filter(name__iexact="working").first()
    status = StatusOption.objects.filter(name__iexact="available").first()
    condition_name = condition.name if condition else "Working"
    status_name = status.name if status else "Available"

    sheet.append(["XLR Cable 10m", "XLR-10M", "Balanced audio cable", "Cables", "XLR", first_room, 5, 0, 0, condition_name, status_name])
    sheet.append(["XLR Cable 10m", "XLR-10M", "Balanced audio cable", "Cables", "XLR", second_room, 2, 0, 0, condition_name, status_name])
    sheet.append(["Dynamic Microphone", "", "Vocal microphone", "Mics", "Dynamic", first_room, 3, 0, 0, condition_name, status_name])
    for index, width in enumerate([28, 14, 30, 14, 14, 18, 10, 12, 20, 12, 12], start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"

    lists = workbook.create_sheet("Lists")
    lists.append(["Location", "Condition", "Status"])
    for cell in lists[1]:
        cell.font = Font(bold=True)
    conditions = list(ConditionOption.objects.filter(is_active=True).order_by("name").values_list("name", flat=True))
    statuses = list(StatusOption.objects.filter(is_active=True).order_by("name").values_list("name", flat=True))
    for row in zip_longest(locations, conditions, statuses, fillvalue=""):
        lists.append(list(row))
    for index, width in enumerate([20, 20, 20], start=1):
        lists.column_dimensions[get_column_letter(index)].width = width

    lists.append([])
    lists.append(["Use one row per item per room. The Location, Condition and Status columns must match the names above.", "", ""])
    lists.append(["Leave the SKU blank and one will be generated from the item name automatically.", "", ""])
    lists.append(["Quantity Out and Quantity Maintenance are optional and default to 0.", "", ""])
    return workbook
