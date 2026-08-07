from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from django.http import HttpResponseRedirect
from django.contrib import messages


class KwenaSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        if sociallogin.account.provider == "google":
            email = (sociallogin.account.extra_data.get("email") or "").lower()
            if not email.endswith("@kwenamusic.co.za"):
                messages.error(
                    request,
                    "Only @kwenamusic.co.za Google accounts are allowed to sign in.",
                )
                raise ImmediateHttpResponse(
                    HttpResponseRedirect("/inventory/landing/")
                )

    def is_open_for_signup(self, request, sociallogin):
        if getattr(sociallogin, "account", None) and sociallogin.account.provider == "google":
            email = (sociallogin.account.extra_data.get("email") or "").lower()
            if not email.endswith("@kwenamusic.co.za"):
                return False
        return super().is_open_for_signup(request, sociallogin)

    def is_auto_signup_allowed(self, request, sociallogin):
        if getattr(sociallogin, "account", None) and sociallogin.account.provider == "google":
            email = (sociallogin.account.extra_data.get("email") or "").lower()
            if not email.endswith("@kwenamusic.co.za"):
                return False
        return super().is_auto_signup_allowed(request, sociallogin)
