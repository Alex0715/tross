"""
Helpers for extracting a LinkedIn public-profile "slug" (a.k.a. vanity name)
from any of the common URL shapes LinkedIn issues, including localized
domains.

Examples this handles:
    https://www.linkedin.com/in/johndoe/
    https://www.linkedin.com/in/johndoe
    http://linkedin.com/in/johndoe?trk=nav
    https://uk.linkedin.com/in/john-doe-12345678/
    https://de.linkedin.com/in/john-doe-12345678/en
    linkedin.com/in/johndoe
    johndoe                      (a bare slug is accepted as-is)
"""

from __future__ import annotations

import re

# Matches the "/in/<slug>" segment of a LinkedIn profile URL, tolerating:
#   - a scheme (http/https) or none
#   - "www." or a two-letter country subdomain (uk., de., fr., ...)
#   - a trailing slash, query string, fragment, or locale suffix (e.g. /en)
_PROFILE_URL_RE = re.compile(
    r"""
    ^\s*
    (?:https?://)?                 # optional scheme
    (?:[a-z]{2,3}\.)?               # optional subdomain: www. / uk. / de. / m. ...
    linkedin\.com
    /in/
    (?P<slug>[^/?#\s]+)             # the vanity slug itself
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A bare slug: letters, digits, hyphens, underscores, percent-encoding.
_BARE_SLUG_RE = re.compile(r"^[\w\-%]+$")


class InvalidLinkedInURLError(ValueError):
    """Raised when a string can't be resolved to a LinkedIn profile slug."""


def extract_profile_slug(url: str) -> str:
    """
    Extract the public identifier (slug) from a LinkedIn profile URL.

    Accepts full URLs (with or without scheme, with or without a country
    subdomain) as well as a bare slug passed directly.

    Raises:
        InvalidLinkedInURLError: if no slug can be resolved from the input.
    """
    if not url or not url.strip():
        raise InvalidLinkedInURLError("URL must not be empty.")

    candidate = url.strip()

    match = _PROFILE_URL_RE.match(candidate)
    if match:
        slug = match.group("slug")
    elif "linkedin.com" not in candidate.lower() and _BARE_SLUG_RE.match(candidate):
        # Allow callers to pass a bare slug (e.g. "johndoe") directly.
        slug = candidate
    else:
        raise InvalidLinkedInURLError(
            f"Could not extract a profile slug from '{url}'. "
            "Expected something like https://www.linkedin.com/in/<slug>/"
        )

    slug = slug.strip("/")
    if not slug or not _BARE_SLUG_RE.match(slug):
        raise InvalidLinkedInURLError(f"Resolved an invalid slug from '{url}'.")

    return slug
