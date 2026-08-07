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
                    HttpResponseRedirect("/inventory/accounts/login/")
                )
