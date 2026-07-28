from django.db import migrations


def _style_for(text):
    """Pick a sensible style for an existing announcement from its words."""
    t = (text or "").lower()
    if any(w in t for w in ("closed", "warning", "caution", "delay", "down", "out of service", "broken", "maintenance", "unavailable")):
        return "warning"
    if any(w in t for w in ("event", "workshop", "concert", "rehearsal", "show", "session", "meet", "audition", "festival")):
        return "event"
    if any(w in t for w in ("welcome", "thanks", "congrat", "back", "restock", "new", "open", "good news", "arrived")):
        return "success"
    return "info"


def backfill_announcement_style(apps, schema_editor):
    Announcement = apps.get_model("inventory", "Announcement")
    for a in Announcement.objects.all():
        if not a.style or a.style == "info":
            a.style = _style_for((a.title or "") + " " + (a.message or ""))
            # Pin anything that looks like an important, still-active notice.
            a.pinned = a.pinned or any(
                w in (a.title or "").lower() for w in ("important", "urgent", "notice")
            )
            a.save(update_fields=["style", "pinned"])


def reverse_style(apps, schema_editor):
    Announcement = apps.get_model("inventory", "Announcement")
    Announcement.objects.update(style="info", pinned=False)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0027_alter_announcement_options_announcement_pinned_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_announcement_style, reverse_style),
    ]
