from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"

    def ready(self):
        from . import live_signals, signals

        live_signals.connect()

        if getattr(self, "_reminder_scheduler_started", False):
            return
        self._reminder_scheduler_started = True

        from django.conf import settings as django_settings
        if not getattr(django_settings, "REMINDER_SCHEDULER_ENABLED", False):
            return

        import threading

        interval_minutes = getattr(django_settings, "REMINDER_SCHEDULER_INTERVAL_MINUTES", 60)

        def run():
            while True:
                try:
                    from .management.commands.send_reminders import send_reminder_notifications
                    send_reminder_notifications()
                except Exception:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.exception("Reminder scheduler error")
                import time
                time.sleep(interval_minutes * 60)

        t = threading.Thread(target=run, daemon=True)
        t.start()
