from django.dispatch import receiver

from allauth.account.signals import user_signed_up


@receiver(user_signed_up)
def assign_default_role(request, user, **kwargs):
    from .permissions import ensure_profile

    ensure_profile(user, "student")
