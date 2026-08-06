from django.core.exceptions import ValidationError
from django.dispatch import receiver

from allauth.account.signals import user_signed_up
from allauth.socialaccount.signals import pre_social_login


@receiver(pre_social_login)
def restrict_google_login(sender, request, sociallogin, **kwargs):
    if sociallogin.account.provider == "google" and sociallogin.user is None:
        email = sociallogin.account.extra_data.get("email", "")
        if not email.lower().endswith("@kwenamusic.co.za"):
            raise ValidationError(
                "Only @kwenamusic.co.za Google accounts are allowed to sign in."
            )


@receiver(user_signed_up)
def assign_default_role(request, user, **kwargs):
    from .permissions import ensure_profile

    ensure_profile(user, "student")
