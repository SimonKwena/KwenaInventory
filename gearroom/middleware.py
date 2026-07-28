"""Middleware that prevents browsers from caching HTML pages in DEBUG.

The dev server serves templates from disk, but browsers aggressively cache
GET responses, so template edits would not show up without a hard refresh.
This adds no-store headers to every response so pages are always fresh.
"""


class NoCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # The static-file debug server in run_https.py sets its own caching,
        # so only stamp Django-rendered responses.
        if not response.has_header("Cache-Control"):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
        return response
