from django import template

from ..permissions import is_admin_or_above

register = template.Library()


@register.filter
def type_label(value):
    """Turn a transaction_type slug into a friendly label: 'check_out' ->
    'Check Out', 'book_ahead' -> 'Book Ahead', 'check_in' -> 'Check In'."""
    return (value or "").replace("_", " ").title()


@register.filter
def admin_suffix(user):
    """Return " (Admin)" when ``user`` is an admin (or above), else "".

    Used on slip history so a name reads "Simon Steyn (Admin)" when the actor
    was an administrator, and just "Simon Steyn" otherwise."""
    try:
        if user and is_admin_or_above(user):
            return " (Admin)"
    except Exception:
        pass
    return ""


@register.filter
def is_admin(user):
    """True when ``user`` is an admin (or above). Used to hide the redundant
    "Return request raised" slip event when an admin checked the gear back in."""
    try:
        return bool(user and is_admin_or_above(user))
    except Exception:
        return False


@register.filter
def get_item(mapping, key):
    """Lookup ``key`` in a dict-like object, used for per-item return conditions."""
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        try:
            return mapping[key]
        except (KeyError, TypeError, IndexError):
            return None


@register.filter
def qr_data_uri(value, scale=4):
    """Render ``value`` (e.g. a return URL) as an inline SVG QR code data URI.

    Generated on the fly so nothing is stored on disk or in the database."""
    if not value:
        return ""
    import segno

    qr = segno.make(value, error="m")
    svg = qr.svg_data_uri(scale=int(scale), dark="#111827", light="#ffffff")
    return svg
