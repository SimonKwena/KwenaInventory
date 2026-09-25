"""Connect model signals so any data change triggers a live-update broadcast."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import live
from .models import (
    Announcement,
    CatalogItem,
    Maintenance,
    Request,
    RequestItem,
    StockEntry,
    Transaction,
)

# Models whose changes should be reflected on open pages in real time.
# Request/RequestItem back the "Pending requests" list and the pending badge, so
# a newly submitted, approved, rejected or edited request must bump the version
# too — otherwise those regions only refresh on the slow fallback poll.
_TRACKED = (CatalogItem, StockEntry, Transaction, Announcement, Maintenance, Request, RequestItem)


def connect():
    for model in _TRACKED:
        post_save.connect(_bump, sender=model, dispatch_uid=f"live_bump_save_{model.__name__}")
        post_delete.connect(_bump, sender=model, dispatch_uid=f"live_bump_delete_{model.__name__}")


def _bump(sender, instance, **kwargs):
    live.bump()
