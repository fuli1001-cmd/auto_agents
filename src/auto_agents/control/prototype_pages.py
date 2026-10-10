"""Keep frozen state URLs bound to a relative HTML file in the package."""

from urllib.parse import unquote, urlsplit

from .types import ControlError


def page_file(page):
    if not isinstance(page, str) or any(ord(c) < 32 for c in page):
        raise ControlError("prototype_scope", "Unsafe prototype page URL", category="verification")
    try:
        url = urlsplit(page)
    except ValueError as error:
        raise ControlError("prototype_scope", "Unsafe prototype page URL", category="verification") from error
    name = unquote(url.path)
    if (
        url.scheme or url.netloc or "\\" in name
        or any(ord(c) < 32 for c in name)
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or not name.endswith(".html")
    ):
        raise ControlError("prototype_scope", "Prototype pages must stay in their relative HTML package", category="verification")
    return name
