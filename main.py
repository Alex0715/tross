"""
FastAPI service that turns a public LinkedIn profile URL into structured
JSON, by querying LinkedIn's internal Voyager API via the `linkedin-api`
package rather than scraping rendered HTML.

Authentication uses a browser-exported cookie jar (`LINKEDIN_LI_AT` and
`LINKEDIN_JSESSIONID`) — not a username/password login. See README,
"Authentication", for why, and how to obtain those cookies.

Run with:
    uvicorn main:app --reload

See README.md for setup, architecture notes, and known limitations.
"""

from __future__ import annotations

import logging
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.linkedin_client import (
    LinkedInError,
    fetch_raw_profile,
)
from app.mapper import map_profile
from app.schemas import ErrorDetail, ErrorResponse, ProfileResponse
from app.url_utils import InvalidLinkedInURLError, extract_profile_slug

# Load the LinkedIn session cookies from a local .env file, if present.
# In production these should instead be injected as real environment
# variables (or secrets) by the deployment platform.
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("linkedin_profile_api")

app = FastAPI(
    title="LinkedIn Profile API",
    description=(
        "Fetches structured profile data by querying LinkedIn's internal "
        "Voyager API through the `linkedin-api` package, rather than "
        "scraping rendered HTML."
    ),
    version="1.0.0",
)


def error_response(
    *,
    http_status: int,
    code: str,
    message: str,
    upstream_status: Optional[int] = None,
) -> JSONResponse:
    """
    Render a failure in the service's single error shape.

    Everything in the body is either a fixed string or a status code — no
    exception text, no cookie or token, and no fragment of a LinkedIn
    response ever reaches the caller. Diagnostic detail goes to the log.

    `upstream_status` is omitted entirely when there is no upstream status
    to report (e.g. a client-side URL validation failure).
    """
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, upstream_status=upstream_status)
    )
    return JSONResponse(status_code=http_status, content=body.model_dump(exclude_none=True))


@app.get(
    "/profile",
    response_model=ProfileResponse,
    responses={
        400: {"model": ErrorResponse, "description": "INVALID_PROFILE_URL"},
        404: {"model": ErrorResponse, "description": "PROFILE_NOT_FOUND"},
        429: {"model": ErrorResponse, "description": "LINKEDIN_RATE_LIMITED"},
        502: {"model": ErrorResponse, "description": "LINKEDIN_UPSTREAM_ERROR"},
        503: {"model": ErrorResponse, "description": "LINKEDIN_AUTH_FAILED"},
    },
    summary="Fetch a structured LinkedIn profile",
)
def get_profile(
    url: str = Query(
        ...,
        description="A LinkedIn profile URL, e.g. https://www.linkedin.com/in/johndoe/",
        examples=["https://www.linkedin.com/in/johndoe/"],
    ),
):
    try:
        slug = extract_profile_slug(url)
    except InvalidLinkedInURLError as exc:
        logger.info("Rejected malformed profile URL: %s", exc)
        return error_response(
            http_status=400,
            code="INVALID_PROFILE_URL",
            message=(
                "The supplied URL is not a valid LinkedIn profile URL. "
                "Expected something like https://www.linkedin.com/in/<slug>/"
            ),
        )

    try:
        raw_profile = fetch_raw_profile(slug)
    except LinkedInError as exc:
        # Every failure mode carries its own public code, caller-safe message
        # and HTTP status; the exception's own text is logged, never returned.
        logger.info("Profile fetch for '%s' failed: %s (%s)", slug, exc.code, exc)
        return error_response(
            http_status=exc.http_status,
            code=exc.code,
            message=exc.public_message,
            upstream_status=exc.upstream_status,
        )

    try:
        return map_profile(slug, raw_profile)
    except Exception:  # a shape we didn't anticipate slipped through
        logger.exception("Failed to map profile payload for slug '%s'", slug)
        return error_response(
            http_status=502,
            code="LINKEDIN_UPSTREAM_ERROR",
            message="LinkedIn is currently unavailable or returned an unexpected response.",
            upstream_status=502,
        )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # noqa: ARG001
    logger.exception("Unhandled exception")
    return error_response(
        http_status=500,
        code="INTERNAL_ERROR",
        message="An internal error occurred while handling the request.",
    )


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}
