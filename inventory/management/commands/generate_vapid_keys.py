import os

from django.core.management.base import BaseCommand
from django.utils.crypto import get_random_string

from pywebpush import generate_vapid_keys


class Command(BaseCommand):
    help = "Generate VAPID keys for Web Push notifications and print them."

    def handle(self, *args, **options):
        private_key, public_key = generate_vapid_keys()
        self.stdout.write(self.style.SUCCESS("Web Push VAPID keys:"))
        self.stdout.write(f"  Private key: {private_key}")
        self.stdout.write(f"  Public  key: {public_key}")
        self.stdout.write("")
        self.stdout.write("Add these to your .env file:")
        self.stdout.write(f"  WEBPUSH_VAPID_PRIVATE_KEY={private_key}")
        self.stdout.write(f"  WEBPUSH_VAPID_PUBLIC_KEY={public_key}")
        self.stdout.write(f"  WEBPUSH_VAPID_ADMIN_EMAIL=you@example.com")
