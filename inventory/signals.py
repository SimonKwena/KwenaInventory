from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from allauth.account.signals import user_signed_up


@receiver(user_signed_up)
def assign_default_role(request, user, **kwargs):
    from .permissions import ensure_profile

    ensure_profile(user, "student")


@receiver(user_logged_in)
def flag_onboarding_on_login(sender, request, user, **kwargs):
    """Stash a session flag so the first-login onboarding popup can render.

    Fires on every successful login (Django, allauth Google, local). The modal
    itself is gated by this session flag, which is cleared when the user
    dismisses the popup (see ``views.mark_onboarding_seen``). The persistent
    ``has_seen_onboarding`` flag on the profile prevents the modal from
    re-appearing on future logins once a user has dismissed it permanently.
    """
    if not getattr(user, "is_authenticated", False):
        return
    profile = getattr(user, "user_profile", None)
    if profile is not None and profile.has_seen_onboarding:
        return
    request.session["show_onboarding"] = True
