"""
Thin wrapper around LinkedIn's Voyager API.

Responsible for:
  - Building an authenticated session from a **browser-exported cookie jar**
    supplied via the environment — never hardcoded, never a username/password
    login (see "Why cookies, not a password login" below).
  - Fetching the raw profile payload for a given slug — via the
    `/identity/dash/profiles` decoration endpoint, called directly against
    the authenticated session, rather than via `linkedin_api`'s own
    `get_profile()` (see note below on why).
  - Translating the various failure modes into a small, predictable set of
    exceptions carrying a stable public error code, a caller-safe message,
    and the upstream HTTP status. Nothing that reaches the client ever
    contains a cookie, token, credential, or raw LinkedIn response body.

Why cookies, not a password login:
`linkedin_api.Linkedin(email, password)` authenticates through LinkedIn's
mobile `/uas/authenticate` endpoint. The `li_at` that flow issues is revoked
server-side within roughly four to five seconds — verified by timing a single
login against `/voyager/api/me`:

    t+0.0s -> 200    t+6.0s  -> 401
    t+3.0s -> 200    t+9.0s+ -> 401

The cookie's own `expires` attribute claims 2027, so nothing local detects
the revocation; the next call simply 401s. Because `linkedin_api._fetch`
sleeps 2-5 seconds before *every* request, that window is usually gone before
the first real call lands — which is why the failure looked intermittent.
A `li_at` taken from a logged-in browser session is not subject to this and
lasts weeks, which is why the same account works fine in a browser.

Why call `/identity/dash/profiles` directly instead of `client.get_profile()`:
`linkedin_api.get_profile()` targets the legacy
`/identity/profiles/{id}/profileView` endpoint, which LinkedIn has been
sunsetting — see README.md, "Postmortem: the 410 Gone on /profileView".
LinkedIn's current web client instead reads profiles through
`/identity/dash/profiles`, a "dash" REST endpoint that takes a
`decorationId` describing which fields to project, rather than an
internal, frequently-rotated GraphQL `queryId`. This was verified
end-to-end against a live account before being wired in here.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from linkedin_api import Linkedin
from requests.cookies import RequestsCookieJar
from requests.exceptions import HTTPError, TooManyRedirects

logger = logging.getLogger("linkedin_profile_api")

# Decoration recipe LinkedIn's own web client uses to render a full profile
# page. Like any internal, undocumented identifier this can change without
# notice — see README's "Known limitations" on schema drift.
_PROFILE_DECORATION_ID = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93"

_DASH_PROFILE_ACCEPT_HEADER = "application/vnd.linkedin.normalized+json+2.1"

# Cookie domain LinkedIn itself sets these on. The leading dot makes them
# match `www.linkedin.com`, which is where every Voyager call goes.
_COOKIE_DOMAIN = ".linkedin.com"

# The two cookies a Voyager session actually needs:
#   li_at      — the member session token; this is the secret that matters.
#   JSESSIONID — doubles as the CSRF token; LinkedIn rejects any Voyager
#                request whose `csrf-token` header doesn't match this cookie.
_LI_AT_ENV = "LINKEDIN_LI_AT"
_JSESSIONID_ENV = "LINKEDIN_JSESSIONID"


class LinkedInError(RuntimeError):
    """
    Base class for every failure this module reports.

    Carries the public contract for the error — a stable `code`, a message
    that is safe to hand to an API caller verbatim, and the HTTP status our
    own API should answer with — separately from the exception's internal
    string, which may contain diagnostic detail and is only ever logged.
    """

    code = "LINKEDIN_UPSTREAM_ERROR"
    public_message = "LinkedIn is currently unavailable or returned an unexpected response."
    http_status = 502

    def __init__(self, detail: str = "", *, upstream_status: Optional[int] = None) -> None:
        super().__init__(detail or self.public_message)
        # The status LinkedIn returned, when we have one. Reported to callers
        # as `error.upstream_status`; it is a status code, never a body.
        self.upstream_status = upstream_status


class LinkedInAuthError(LinkedInError):
    """
    Raised when LinkedIn refuses the session: the cookie jar is missing,
    malformed, expired, revoked, or the account hit an access restriction.
    """

    code = "LINKEDIN_AUTH_FAILED"
    public_message = (
        "LinkedIn rejected the authenticated session. The session may have expired, "
        "been invalidated, or reached an account/access restriction."
    )
    # Our credentials are the problem, not the caller's request.
    http_status = 503


class LinkedInProfileNotFoundError(LinkedInError):
    """Raised when the requested profile slug does not resolve to a profile."""

    code = "PROFILE_NOT_FOUND"
    public_message = "The requested LinkedIn profile could not be found or is not accessible."
    http_status = 404


class LinkedInRateLimitedError(LinkedInError):
    """Raised when LinkedIn throttles us."""

    code = "LINKEDIN_RATE_LIMITED"
    public_message = "LinkedIn temporarily rate-limited the request. Please try again later."
    http_status = 429


class LinkedInUpstreamError(LinkedInError):
    """
    Raised for any other unexpected failure talking to LinkedIn.

    When we have no status from LinkedIn at all — a timeout, DNS failure,
    unparseable body — we still report an `upstream_status`, defaulting to
    the 502 we answer with, so the field's presence is predictable for this
    error code.
    """

    def __init__(self, detail: str = "", *, upstream_status: Optional[int] = None) -> None:
        super().__init__(detail, upstream_status=upstream_status or self.http_status)


# Process-wide client cache. Deliberately *not* `@lru_cache`: that memoises
# permanently, so a client whose session has since gone stale (expired
# cookies, server-side session revocation) would be handed out forever with
# no way to evict it. This cache is explicitly invalidatable via
# `reset_client()`, which callers do whenever LinkedIn tells us the session
# is no longer good.
_client: Linkedin | None = None
_client_lock = threading.Lock()


def reset_client() -> None:
    """Drop the cached client so the next `get_client()` rebuilds the session."""
    global _client
    with _client_lock:
        _client = None


def _normalize_jsessionid(raw: str) -> str:
    """
    Return JSESSIONID in the quoted form LinkedIn expects as a cookie value.

    Browsers display this cookie as `"ajax:1234567890123456789"` — the double
    quotes are part of the value, and LinkedIn 401s if they are missing. Users
    copy it both with and without them, so accept either and re-add them.
    (`linkedin_api` strips them again for the `csrf-token` header, which must
    be unquoted — the two forms are not interchangeable.)
    """
    value = raw.strip().strip('"').strip()
    return f'"{value}"'


def _build_cookie_jar() -> RequestsCookieJar:
    """
    Assemble a `RequestsCookieJar` from the browser-exported cookies in the
    environment.

    Raises:
        LinkedInAuthError: if either required cookie is absent or blank.
    """
    li_at = (os.getenv(_LI_AT_ENV) or "").strip()
    jsessionid = (os.getenv(_JSESSIONID_ENV) or "").strip()

    missing = [name for name, value in ((_LI_AT_ENV, li_at), (_JSESSIONID_ENV, jsessionid)) if not value]
    if missing:
        # Names of the missing variables only — never any value.
        raise LinkedInAuthError(
            f"Missing required LinkedIn session cookie(s): {', '.join(missing)}. "
            "Export them from a logged-in browser session into your .env file "
            "(see README, 'Authentication')."
        )

    jar = RequestsCookieJar()
    jar.set("li_at", li_at, domain=_COOKIE_DOMAIN, path="/", secure=True)
    jar.set("JSESSIONID", _normalize_jsessionid(jsessionid), domain=_COOKIE_DOMAIN, path="/", secure=True)
    return jar


def get_client() -> Linkedin:
    """
    Build (and cache) a `Linkedin` client backed by the browser-exported
    cookie jar. No username/password login is performed, and no automated
    re-login happens on failure — an invalid jar is reported, not worked
    around.

    We only use this client for its authenticated `requests.Session`
    (cookies + CSRF token); the actual profile fetch bypasses its
    `get_profile()` method — see module docstring.
    """
    global _client

    if _client is not None:
        return _client

    jar = _build_cookie_jar()

    with _client_lock:
        if _client is not None:
            return _client
        try:
            # `cookies=` is the library's supported cookie-auth path: it skips
            # the login flow entirely and seeds the session from this jar,
            # setting the `csrf-token` header from JSESSIONID. Username and
            # password are unused on this path, so they stay empty rather
            # than being read from the environment at all.
            client = Linkedin("", "", cookies=jar)
        except Exception as exc:  # the library doesn't expose a narrow exception type
            logger.exception("Failed to build a LinkedIn session from the supplied cookies")
            raise LinkedInAuthError(
                "Could not construct a LinkedIn session from the supplied cookies. "
                "They may be malformed — check that JSESSIONID looks like \"ajax:...\"."
            ) from exc

        # Belt and braces: the library derives `csrf-token` from JSESSIONID,
        # but every Voyager call 401s without it, so fail loudly and locally
        # rather than as a confusing upstream error later.
        if not client.client.session.headers.get("csrf-token"):
            raise LinkedInAuthError("Session was built without a csrf-token header.")

        _client = client
        return client


def fetch_raw_profile(slug: str) -> dict[str, Any]:
    """
    Fetch the raw, normalized `{data, included}` profile payload for `slug`
    from `/identity/dash/profiles`.

    Raises:
        LinkedInAuthError: LinkedIn rejected the session (401/403).
        LinkedInProfileNotFoundError: the slug doesn't correspond to a profile.
        LinkedInRateLimitedError: LinkedIn throttled the request (429).
        LinkedInUpstreamError: any other failure reaching/parsing the API.
    """
    client = get_client()

    try:
        response = client._fetch(
            "/identity/dash/profiles",
            params={
                "q": "memberIdentity",
                "memberIdentity": slug,
                "decorationId": _PROFILE_DECORATION_ID,
            },
            headers={"accept": _DASH_PROFILE_ACCEPT_HEADER},
        )
        response.raise_for_status()
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (400, 404, 410):
            raise LinkedInProfileNotFoundError(
                f"No LinkedIn profile found for slug '{slug}'."
            ) from exc
        if status in (401, 403):
            # The session, not the request, is the problem — evict the cached
            # client so the next call rebuilds from the (possibly refreshed)
            # cookie jar instead of reusing one LinkedIn has already rejected.
            reset_client()
            logger.warning("LinkedIn rejected the session (%s); cached client evicted.", status)
            raise LinkedInAuthError(
                f"LinkedIn rejected the current session ({status}).", upstream_status=status
            ) from exc
        if status == 429:
            logger.warning("Rate limited by LinkedIn fetching profile '%s'", slug)
            raise LinkedInRateLimitedError(
                "LinkedIn rate-limited the request.", upstream_status=status
            ) from exc
        logger.exception("Upstream HTTP error fetching profile '%s'", slug)
        raise LinkedInUpstreamError(
            f"LinkedIn API request failed ({status}).", upstream_status=status
        ) from exc
    except TooManyRedirects as exc:
        # An unauthenticated Voyager call isn't always answered with a 401:
        # LinkedIn frequently redirects it to the login page instead, which
        # then redirects onward until `requests` gives up. That is an auth
        # failure wearing a transport-error costume, so report it as one —
        # otherwise an expired cookie surfaces as an opaque 502 and sends
        # people looking for a LinkedIn outage that isn't happening.
        reset_client()
        logger.warning(
            "Voyager call for '%s' hit a redirect loop (login redirect); "
            "treating as an expired session and evicting the cached client.",
            slug,
        )
        raise LinkedInAuthError(
            "LinkedIn redirected the Voyager request to a login page, which "
            "means the session cookies are no longer valid."
        ) from exc
    except LinkedInError:
        raise
    except Exception as exc:  # network errors, DNS, TLS, timeouts
        logger.exception("Unexpected error fetching profile '%s'", slug)
        raise LinkedInUpstreamError("Unexpected error fetching profile from LinkedIn.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        logger.exception("Non-JSON response fetching profile '%s'", slug)
        raise LinkedInUpstreamError("LinkedIn returned a non-JSON response.") from exc

    data = payload.get("data") or {}
    elements = data.get("*elements") or []
    included = payload.get("included") or []

    # A 200 whose body carries an error status is LinkedIn's way of reporting
    # an unauthenticated session on some decoration endpoints — treat it as
    # the auth failure it is rather than as a missing profile.
    body_status = data.get("status")
    if body_status in (401, 403):
        reset_client()
        logger.warning("LinkedIn returned an in-body %s; cached client evicted.", body_status)
        raise LinkedInAuthError(
            f"LinkedIn rejected the current session (in-body {body_status}).",
            upstream_status=body_status,
        )

    if not elements or not included:
        raise LinkedInProfileNotFoundError(f"No LinkedIn profile found for slug '{slug}'.")

    return {"root_urn": elements[0], "included": included}
