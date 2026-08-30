# tross — LinkedIn Profile API

Give it a LinkedIn profile URL, get back clean JSON: name, headline,
location, about, experience, education, skills, certifications, languages,
and image URLs.

It doesn't scrape rendered HTML. It authenticates with **session cookies
exported from your own logged-in browser** and talks to LinkedIn's internal
**Voyager API** — the same JSON API linkedin.com's own frontend calls —
through the community
[`linkedin-api`](https://github.com/tomquirk/linkedin-api) package.

---

## Read this first

Automated access to LinkedIn's private Voyager API violates LinkedIn's
[User Agreement](https://www.linkedin.com/legal/user-agreement). Realistically
that means:

- the account whose session you use is at genuine risk of being restricted or
  permanently banned,
- depending on your jurisdiction and what you do with the data, there may be
  legal exposure,
- it's fine for learning, personal experimentation, and take-home challenges
  against your own account and public data — it is not something to run at
  scale or against other people's accounts without their consent.

Use a throwaway or secondary LinkedIn account. Not your real one.

---

## How the project is put together

The whole thing is a straight pipeline, and the folder layout mirrors it:

```
URL string
   │  app/url_utils.py      pull the profile slug out of whatever URL shape you passed
   ▼
slug
   │  app/linkedin_client.py build a cookie-authenticated session, call Voyager,
   │                         turn every failure into a typed exception
   ▼
raw normalized payload  {root_urn, included: [...]}
   │  app/mapper.py         flatten LinkedIn's entity graph into our own schema
   ▼
ProfileResponse           app/schemas.py — the public contract
   │  main.py              FastAPI route, HTTP status mapping, error rendering
   ▼
JSON
```

```
.
├── main.py                  # FastAPI app: one route, error → HTTP mapping
├── diagnose.py              # read-only auth/session/endpoint diagnostic script
├── app/
│   ├── url_utils.py         # URL → slug, with a regex that tolerates LinkedIn's URL zoo
│   ├── linkedin_client.py   # cookie auth, cached session, raw Voyager fetch, typed errors
│   ├── mapper.py            # raw Voyager entity graph → clean schema
│   └── schemas.py           # Pydantic models for both success and error shapes
├── requirements.txt
├── .env.example             # template for the two cookies you need
└── README.md
```

A few deliberate choices behind that layout:

**Each module owns one failure mode.** `url_utils` only ever raises
`InvalidLinkedInURLError`. `linkedin_client` only ever raises subclasses of
`LinkedInError`, and each subclass carries its own public error code, its own
caller-safe message, and the HTTP status the API should answer with. `main.py`
therefore doesn't need to know *why* something failed — it just renders
whatever the exception already decided. Adding a new failure mode is a new
exception class, not a new `if` in the route.

**Secrets and internals never cross the boundary.** Error bodies are fixed
strings plus a status code. Cookies, tokens, exception text, and raw LinkedIn
payloads are logged server-side and never serialized into a response.

**The mapper assumes the upstream is unreliable.** Voyager is undocumented and
can change between LinkedIn deploys, so every field goes through
`.get(..., default)`, every schema field is `Optional`, and a shape we didn't
anticipate degrades to `null` rather than throwing.

**The session is cached, but evictable.** `get_client()` memoizes the
authenticated client behind a lock — but not with `@lru_cache`, deliberately,
because that would hand out a dead session forever. A `401`/`403` from
LinkedIn calls `reset_client()`, so the next request rebuilds from the current
environment.

---

## Setup

### 1. Install

```bash
git clone https://github.com/Alex0715/tross.git
cd tross
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Add your cookies

This service authenticates with two cookies from a browser that's already
logged in to LinkedIn. It does **not** log in with an email and password — see
[Authentication](#authentication) for why that path doesn't work at all.

```bash
cp .env.example .env
```

```dotenv
# .env  — never commit this; .gitignore already excludes it
LINKEDIN_LI_AT=AQEDAS...
LINKEDIN_JSESSIONID="ajax:1234567890123456789"
```

To get them: log in to `https://www.linkedin.com`, open DevTools →
**Chrome/Edge:** Application → Storage → Cookies → `https://www.linkedin.com`,
**Firefox:** Storage → Cookies — and copy `li_at` and `JSESSIONID`.

Don't log out of that browser session afterwards; logging out invalidates the
`li_at` you just copied. Closing the tab or the browser is fine.

`main.py` loads `.env` via `python-dotenv` at startup. In a real deployment,
inject both values as platform secrets instead of shipping a file.

### 3. Run

```bash
uvicorn main:app --reload
```

Live at `http://127.0.0.1:8000`. If something looks wrong, `python diagnose.py`
walks the auth → session → endpoint chain and prints where it breaks (cookie
*names* and status codes only, never values).

---

## API documentation

FastAPI generates interactive docs from the same models the code uses:

- Swagger UI — <http://127.0.0.1:8000/docs>
- ReDoc — <http://127.0.0.1:8000/redoc>
- OpenAPI schema — `http://127.0.0.1:8000/openapi.json`

### `GET /profile`

| Param | Type   | Required | Description                                                          |
| ----- | ------ | -------- | -------------------------------------------------------------------- |
| `url` | string | yes      | A LinkedIn profile URL, e.g. `https://www.linkedin.com/in/johndoe/`  |

Accepts localized subdomains (`uk.linkedin.com`, `de.linkedin.com`, …), URLs
with or without a scheme, trailing slash, query string, or locale suffix, and
a bare slug (`johndoe`) passed directly.

```bash
curl "http://127.0.0.1:8000/profile?url=https://www.linkedin.com/in/johndoe/"
```

**200 — success**

```json
{
  "slug": "johndoe",
  "name": "John Doe",
  "headline": "Senior Backend Engineer at Acme Corp",
  "location": "San Francisco Bay Area",
  "about": "I build things.",
  "experience": [
    {
      "title": "Senior Backend Engineer",
      "company": "Acme Corp",
      "location": "Remote",
      "description": "Built stuff.",
      "date_range": { "start": "2021-03", "end": null }
    }
  ],
  "education": [
    {
      "school": "State University",
      "degree": "B.S.",
      "field_of_study": "Computer Science",
      "date_range": { "start": "2013", "end": "2017" }
    }
  ],
  "skills": ["Python", "FastAPI"],
  "certifications": [
    {
      "name": "AWS Certified",
      "authority": "Amazon",
      "time_period": "2022",
      "url": "https://aws.example"
    }
  ],
  "languages": [{ "name": "English", "proficiency": "NATIVE_OR_BILINGUAL" }],
  "images": {
    "profile_picture": "https://media.licdn.com/dms/image/.../400_400/pic.jpg",
    "background_picture": null
  }
}
```

Field notes: `date_range.end` is `null` for a current role. Any field the
profile doesn't expose (or the payload doesn't project) comes back `null` or
as an empty list — the keys are always present.

### `GET /health`

Returns `{"status": "ok"}`. Not in the OpenAPI schema; it's for liveness
probes.

### Errors

Every failure shares one shape. `code` is stable and safe to branch on;
`message` is human-readable and deliberately generic; `upstream_status` appears
only when the failure actually came from LinkedIn.

```json
{
  "success": false,
  "error": {
    "code": "LINKEDIN_AUTH_FAILED",
    "message": "LinkedIn rejected the authenticated session. The session may have expired, been invalidated, or reached an account/access restriction.",
    "upstream_status": 401
  }
}
```

| HTTP  | `code`                    | `upstream_status`           | When                                                                     |
| ----- | ------------------------- | --------------------------- | ------------------------------------------------------------------------ |
| `400` | `INVALID_PROFILE_URL`     | omitted                     | `url` isn't a resolvable LinkedIn profile URL or slug                    |
| `404` | `PROFILE_NOT_FOUND`       | omitted                     | Slug doesn't resolve, or isn't visible to this session                   |
| `429` | `LINKEDIN_RATE_LIMITED`   | `429`                       | LinkedIn throttled the request                                           |
| `502` | `LINKEDIN_UPSTREAM_ERROR` | LinkedIn's status, or `502` | Unexpected/unparseable payload, or a transient upstream/network failure   |
| `503` | `LINKEDIN_AUTH_FAILED`    | `401`/`403`, or omitted     | LinkedIn rejected the session, or no cookies are configured              |
| `500` | `INTERNAL_ERROR`          | omitted                     | Anything unanticipated — logged server-side with a stack trace           |

Two things worth calling out:

- **Auth failure answers `503`, not `401`.** A `401` would imply the *caller*
  failed to authenticate. The truth is that *our* LinkedIn session is bad,
  which makes the service temporarily unavailable to everyone. LinkedIn's own
  status is reported separately as `upstream_status`, and omitted entirely when
  the cookies are simply missing (nothing upstream was contacted).
- **A `401`/`403` also evicts the cached client**, so the next request rebuilds
  the session from the current environment rather than reusing one LinkedIn has
  already rejected.

---

## Approach

### Why the Voyager API instead of scraping the DOM

LinkedIn's frontend is a single-page app: it renders almost nothing
server-side and populates itself by calling internal JSON endpoints under
`/voyager/api/...`. Talking to those endpoints directly is strictly better
than parsing the page they produce:

- **Structured data, not markup.** Voyager returns typed JSON keyed by stable
  field names (`firstName`, `headline`, `dateRange`, …) instead of
  `<div class="pv-top-card…">` soup. `mapper.py` maps *fields*, not CSS
  selectors, so a visual redesign doesn't break it.
- **No headless browser.** No Selenium/Playwright, no waiting for hydration,
  no coaxing lazy-loaded sections into existence. It's a handful of
  authenticated HTTP calls — fast, light, and container-friendly.
- **More data.** Paginated skills, full experience descriptions, and image CDN
  URLs at multiple resolutions aren't reliably in the rendered HTML.

The trade-off: it's an unofficial API with no support contract, and it needs an
authenticated session rather than being anonymous.

### Authentication: why cookies and not email + password

`linkedin-api` can log in with a username and password, and this project
originally did. That path is broken from LinkedIn's side.

`Linkedin(email, password)` authenticates through LinkedIn's mobile
`/uas/authenticate` endpoint. The login *succeeds* — no CAPTCHA, no challenge,
no restriction — and the `li_at` it returns works. It just stops working almost
immediately. Timing a single login against `/voyager/api/me`, with no second
login in between:

```
t+0.0s  /me → 200        t+6.0s  /me → 401
t+3.0s  /me → 200        t+9.0s+ /me → 401
```

The session is revoked server-side after roughly four to five seconds. The
cookie's own `expires` attribute claims 2027, so nothing locally can tell it
died — the next call just `401`s. And `linkedin_api._fetch` sleeps 2–5 seconds
before *every* request, so that window is usually gone before the first real
call lands, which is why the failure looked intermittent rather than total.

A `li_at` from a normal browser login isn't treated this way and lasts weeks.
That's exactly why the same account works fine in a browser while the API
client `401`s. `diagnose.py` reproduces all of this end to end.

**Two cookies are required.** `li_at` is the member session token — the secret
that matters. `JSESSIONID` doubles as the CSRF token; LinkedIn rejects any
Voyager request whose `csrf-token` header doesn't match it. There's a subtlety:
the cookie must keep its surrounding double quotes, while the header must not
have them. The client normalizes this, so paste the value either way.

### The `410 Gone` on `/profileView`, and the fix

Early on, `GET /profile` failed with a `502` wrapping a `KeyError` from deep
inside `linkedin_api.get_profile()`. Tracing it:

1. `get_profile()` calls the legacy endpoint
   `/identity/profiles/{id}/profileView`.
2. LinkedIn now answers that with `HTTP 410 Gone` and an empty
   `{"status": 410}` body — no `message` field.
3. The library assumes any non-200 status response carries `data["message"]`,
   so it throws an unhandled `KeyError` rather than failing cleanly.

LinkedIn has been sunsetting `profileView` in favour of newer Dash endpoints,
and `linkedin-api` hasn't caught up. The fork `open-linkedin-api` was evaluated
as a drop-in replacement and **rejected**: its published source shows a
`get_profile()` unchanged from upstream — same `/profileView` call, same
missing-`message` bug, down to the same source comment. It would fail
identically. (For due diligence: it wasn't malicious, just mislabeled.)

The actual fix was to bypass `get_profile()` entirely and call the endpoint
LinkedIn's own web client uses:

```
GET /voyager/api/identity/dash/profiles
    ?q=memberIdentity
    &memberIdentity={slug}
    &decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93
Accept: application/vnd.linkedin.normalized+json+2.1
```

A `decorationId` is a server-registered "which fields to project" recipe.
Unlike the GraphQL routes elsewhere in LinkedIn's frontend, this doesn't depend
on an internal, frequently-rotated `queryId`, which makes it a more stable
target. We still use `linkedin-api` — for the authenticated session it builds
(cookies + CSRF header) — just not its profile method.

The response is a Redux-normalized entity graph: `{"data": …, "included": […]}`,
where profile, positions, education, and skills all arrive as separate typed
entities in one flat list, cross-referencing each other by `entityUrn` instead
of nesting. `mapper.py` re-assembles that graph — `_by_type` groups entities by
their `$type` suffix — into the same `ProfileResponse` schema as before. This
was verified against a live account, returning a fully populated profile for a
slug that consistently `410`'d on the legacy endpoint.

### What this deliberately doesn't do

No proxy rotation, no fingerprint spoofing, no CAPTCHA bypass, no automated
re-login, no retry loops against auth failures. When LinkedIn says no, the
service surfaces a structured error and stops. Silent retries against an auth
failure make detection *more* likely, not less.

---

## Known limitations

- **The whole thing stops working when `li_at` or `JSESSIONID` expires.** This
  is the limitation you'll hit first and most often. These are ordinary browser
  session cookies: they expire on their own schedule, and they're invalidated
  immediately if you log out of that browser session, change the account
  password, or LinkedIn decides to rotate the session. When that happens every
  request returns `503 LINKEDIN_AUTH_FAILED` and there is **no recovery path in
  code** — by design, since there's no automated re-login. The fix is manual:
  re-export both cookies from a logged-in browser into `.env` and restart the
  server. Anything built on top of this API needs to expect that outage.
- **It violates LinkedIn's Terms of Service.** Educational/challenge use only —
  see the warning at the top.
- **Account bans are a real risk.** LinkedIn detects automated Voyager traffic
  by request pattern, timing, and volume, and will lock or permanently ban the
  authenticating account. Reusing a personal session can't fully avoid this.
- **Password login doesn't work at all.** Measured, not assumed — LinkedIn
  revokes that session within seconds. Browser cookies are the only workable
  path.
- **Rate limits.** LinkedIn throttles Voyager traffic per account, surfaced as
  `429 LINKEDIN_RATE_LIMITED`. Fetching many profiles quickly is the single
  biggest driver of both throttling and bans; add your own delay and backoff if
  you're iterating over a list.
- **Schema drift.** Voyager is internal and undocumented; the response shape
  and the `decorationId` above can change without notice between LinkedIn
  deploys. `mapper.py` is defensive, but a big enough change still means
  updating it.
- **`location` is approximate.** The current decoration doesn't project a
  human-readable name onto the profile's `Geo` entity — only `entityUrn` and
  `countryCode`. Rather than making an extra unverified lookup, `location`
  falls back to the most recently listed position's location name, which the
  decoration *does* project. Profiles with no positions get `null`. Disclosed
  rather than silently approximated.
- **Visibility rules still apply.** You get back only what the authenticating
  account is allowed to see — the target's privacy settings, connection degree,
  and LinkedIn's gating of full-profile data for out-of-network viewers all
  still hold.
- **No tests, no pagination, no caching of results.** Single-purpose service,
  one endpoint, no persistence layer.
- **No official support.** `linkedin-api` is community-maintained and can break
  whenever LinkedIn changes its login flow or Voyager schema, with no SLA.
