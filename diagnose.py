"""
Read-only diagnostic for the LinkedIn Voyager integration.

Answers one question: when a profile fetch fails, is the problem
(a) authentication, (b) session handling, or (c) the
`/identity/dash/profiles` endpoint itself?

It does this by walking the same path the service does, one step at a time,
printing the exact HTTP status at each hop:

    1. build the client (authenticate)
    2. inspect the resulting session — cookie *names* and metadata only
    3. GET /me                     -> is the session authenticated at all?
    4. GET /identity/dash/profiles -> does the profile endpoint work?

No retries, no proxies, no evasion. Credentials, cookie values and CSRF
tokens are never printed.

Usage:
    python diagnose.py [profile-url-or-slug]
"""

from __future__ import annotations

import sys
import time

from dotenv import load_dotenv

from app.linkedin_client import (
    _DASH_PROFILE_ACCEPT_HEADER,
    _PROFILE_DECORATION_ID,
    LinkedInAuthError,
    get_client,
    reset_client,
)
from app.url_utils import InvalidLinkedInURLError, extract_profile_slug

DEFAULT_PROFILE = "https://www.linkedin.com/in/williamhgates/"

# Response bodies can contain personal data and, on error pages, session
# echoes. Print a short prefix only, and never a full payload.
_BODY_PREVIEW_CHARS = 400

# How long to wait before re-checking /me. Long enough to catch a session
# that LinkedIn revokes almost immediately after issuing it.
_LIFETIME_PROBE_DELAY = 8

# Headers that are safe to echo verbatim. Anything not on this list is
# reported as present/absent only.
_SAFE_RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "x-li-uuid",
    "x-restli-protocol-version",
    "location",
)


def _rule(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def _preview(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > _BODY_PREVIEW_CHARS:
        return text[:_BODY_PREVIEW_CHARS] + f" ... [+{len(text) - _BODY_PREVIEW_CHARS} chars]"
    return text or "<empty body>"


def _report_response(response) -> None:
    """Print sanitized facts about a response: status, safe headers, body shape."""
    print(f"  HTTP status : {response.status_code} {response.reason or ''}".rstrip())
    print(f"  final URL   : {response.url.split('?')[0]}  (query stripped)")
    if response.history:
        print(f"  redirects   : {[r.status_code for r in response.history]}")

    for name in _SAFE_RESPONSE_HEADERS:
        if name in response.headers:
            print(f"  {name:<12}: {response.headers[name]}")

    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            payload = response.json()
        except ValueError:
            print("  body        : declared JSON but did not parse")
            print(f"  body preview: {_preview(response.text)}")
            return
        if isinstance(payload, dict):
            print(f"  JSON keys   : {sorted(payload)}")
            data = payload.get("data")
            if isinstance(data, dict):
                print(f"  data keys   : {sorted(data)}")
                elements = data.get("*elements")
                if elements is not None:
                    print(f"  *elements   : {len(elements)} entry/entries")
            included = payload.get("included")
            if included is not None:
                print(f"  included    : {len(included)} entry/entries")
            for key in ("status", "code", "message", "serviceErrorCode"):
                if key in payload:
                    print(f"  error.{key:<6}: {payload[key]!r}")
        else:
            print(f"  JSON type   : {type(payload).__name__}")
    else:
        print(f"  body preview: {_preview(response.text)}")


def main() -> int:
    load_dotenv()

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    target = args[0] if args else DEFAULT_PROFILE

    try:
        slug = extract_profile_slug(target) if "linkedin.com" in target else target
    except InvalidLinkedInURLError as exc:
        print(f"Not a usable profile URL: {exc}")
        return 2

    print(f"target slug        : {slug}")

    # --- Step 1: authenticate -------------------------------------------
    _rule("STEP 1: build client / authenticate")
    reset_client()
    try:
        client = get_client()
    except LinkedInAuthError as exc:
        print(f"  FAILED: {exc}")
        cause = exc.__cause__
        if cause is not None:
            print(f"  underlying: {type(cause).__name__}: {cause}")
        print("\nVERDICT: authentication — never got a session.")
        return 1
    print("  OK: client constructed, session established.")

    # --- Step 2: session metadata (names only, never values) ------------
    _rule("STEP 2: session metadata (no credentials/tokens printed)")
    session = client.client.session
    cookie_names = sorted({c.name for c in session.cookies})
    print(f"  cookie names   : {cookie_names}")
    for required in ("li_at", "JSESSIONID"):
        print(f"  has {required:<11}: {required in cookie_names}")
    print(f"  csrf-token set : {'csrf-token' in session.headers}")
    print("  cookie source  : browser-exported jar (LINKEDIN_LI_AT / LINKEDIN_JSESSIONID)")
    metadata = getattr(client.client, "metadata", {}) or {}
    print(f"  metadata keys  : {sorted(metadata)}")

    # --- Step 3: /me — is the session actually authenticated? -----------
    _rule("STEP 3: GET /voyager/api/me")
    me = client._fetch("/me")
    _report_response(me)
    me_ok = me.status_code == 200

    # --- Step 3b: is the session still alive a few seconds later? -------
    # Not a retry — a measurement. A session that authenticates and then
    # dies within seconds looks identical to "bad credentials" at the call
    # site, so we time it explicitly.
    if me_ok:
        _rule(f"STEP 3b: GET /me again after {_LIFETIME_PROBE_DELAY}s (liveness, not retry)")
        time.sleep(_LIFETIME_PROBE_DELAY)
        me_again = client.client.session.get(f"{client.client.API_BASE_URL}/me")
        print(f"  HTTP status : {me_again.status_code} {me_again.reason or ''}".rstrip())
        session_short_lived = me_again.status_code != 200
        if session_short_lived:
            print(f"  Session was accepted at t=0 but rejected {_LIFETIME_PROBE_DELAY}s later.")
    else:
        session_short_lived = False

    # --- Step 4: the profile endpoint the scraper actually uses ---------
    _rule("STEP 4: GET /voyager/api/identity/dash/profiles")
    print(f"  q=memberIdentity, memberIdentity={slug}")
    print(f"  decorationId={_PROFILE_DECORATION_ID}")
    profile = client._fetch(
        "/identity/dash/profiles",
        params={
            "q": "memberIdentity",
            "memberIdentity": slug,
            "decorationId": _PROFILE_DECORATION_ID,
        },
        headers={"accept": _DASH_PROFILE_ACCEPT_HEADER},
    )
    _report_response(profile)

    # --- Verdict ---------------------------------------------------------
    _rule("VERDICT")
    if not me_ok:
        print(f"  /me returned {me.status_code} -> the SESSION is not authenticated.")
        print("  The profile endpoint result below is meaningless until this is fixed.")
        if me.status_code in (401, 403):
            print("  The exported cookie jar is stale or rejected. Re-export li_at and")
            print("  JSESSIONID from a logged-in browser session (README, 'Authentication').")
        return 1

    if session_short_lived:
        print("  /me returned 200 at t=0 but 401 seconds later.")
        print("  -> SESSION HANDLING. Credentials are valid; the session LinkedIn")
        print("     issues to this login flow is revoked almost immediately, so any")
        print("     cached or reused client is dead by the time it is called again.")
        return 1

    print("  /me returned 200 and stayed valid -> auth and session handling are fine.")
    if profile.status_code == 200:
        print("  /identity/dash/profiles returned 200 -> the endpoint works too.")
        print("  If the service still fails, the problem is payload SHAPE (mapper).")
        return 0

    print(f"  /identity/dash/profiles returned {profile.status_code}")
    print("  -> the PROFILE ENDPOINT is the failure, not auth or the session.")
    if profile.status_code in (400, 404):
        print("  Bad slug, or the decorationId/query params no longer match.")
    elif profile.status_code == 410:
        print("  Endpoint sunset by LinkedIn — the decoration recipe is gone.")
    elif profile.status_code == 429:
        print("  Rate limited.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
