"""Shared data-version counter for pushing live updates to open pages.

The app is served as a single process (see run_https.py), but a database-backed
counter is used so the mechanism is correct even if the app is later run with
multiple workers. Every tracked model change bumps the counter (via signals in
live_signals.py); clients poll ``/live/version/`` and refresh their displayed
data when the number changes. This works on any WSGI server with no streaming,
WebSocket, or extra-service requirements.
"""

from django.db import transaction

from .models import LiveVersion


def bump():
    """Increment the shared version and return the new value."""
    with transaction.atomic():
        obj, _created = LiveVersion.objects.select_for_update().get_or_create(
            pk=1, defaults={"version": 0}
        )
        obj.version += 1
        obj.save(update_fields=["version"])
        return obj.version


def peek_version():
    obj = LiveVersion.objects.filter(pk=1).first()
    return obj.version if obj else 0
