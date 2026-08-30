"""
Maps the raw, normalized profile payload from LinkedIn's
`/identity/dash/profiles` endpoint into our clean `ProfileResponse` schema.

Voyager's "dash" endpoints return data in a flattened, Redux-normalized
shape: a top-level `included` list of typed entities (Profile, Position,
Education, Skill, ...), cross-referencing each other by `entityUrn` instead
of nesting. `_index_by_urn` / `_by_type` below re-assemble that graph into
something we can walk field-by-field.

Every field access assumes the upstream payload is unreliable: keys can be
missing, renamed between LinkedIn deploys, or `None`, so we go through
`.get(..., default)` everywhere rather than direct indexing.
"""

from __future__ import annotations

from typing import Any, Optional

from app.schemas import (
    CertificationEntry,
    DateRange,
    EducationEntry,
    ExperienceEntry,
    LanguageEntry,
    ProfileImages,
    ProfileResponse,
)


def _type_suffix(entity: dict) -> str:
    return (entity.get("$type") or "").rsplit(".", 1)[-1]


def _by_type(included: list[dict], type_name: str) -> list[dict]:
    """All entities in `included` whose `$type` ends in `.{type_name}`."""
    return [e for e in included if isinstance(e, dict) and _type_suffix(e) == type_name]


def _format_date(date_obj: Optional[dict]) -> Optional[str]:
    """Turn a Voyager {'month': 1, 'year': 2020} dict into 'YYYY-MM'."""
    if not isinstance(date_obj, dict):
        return None
    year = date_obj.get("year")
    if not year:
        return None
    month = date_obj.get("month")
    return f"{year:04d}-{month:02d}" if month else f"{year:04d}"


def _date_range(date_range_obj: Optional[dict]) -> Optional[DateRange]:
    if not isinstance(date_range_obj, dict):
        return None
    start = _format_date(date_range_obj.get("start"))
    end = _format_date(date_range_obj.get("end"))
    if start is None and end is None:
        return None
    return DateRange(start=start, end=end)


def _picture_url(picture_obj: Any) -> Optional[str]:
    """
    Dash image objects look like:
        {
          "displayImageReference": {
            "vectorImage": {
              "rootUrl": "https://media.licdn.com/dms/image/.../",
              "artifacts": [{"width": 400, "fileIdentifyingUrlPathSegment": "..."}, ...]
            }
          }
        }
    We pick the largest available artifact and join it with the root URL.
    """
    if not isinstance(picture_obj, dict):
        return None

    vector_image = (picture_obj.get("displayImageReference") or {}).get("vectorImage")
    if not isinstance(vector_image, dict):
        return None

    root_url = vector_image.get("rootUrl")
    artifacts = vector_image.get("artifacts") or []
    if not root_url or not artifacts:
        return None

    largest = max(
        artifacts,
        key=lambda a: a.get("width", 0) if isinstance(a, dict) else 0,
        default=None,
    )
    segment = (largest or {}).get("fileIdentifyingUrlPathSegment")
    return f"{root_url}{segment}" if segment else None


def _map_experience(included: list[dict]) -> list[ExperienceEntry]:
    entries = []
    for item in _by_type(included, "Position"):
        entries.append(
            ExperienceEntry(
                title=item.get("title"),
                company=item.get("companyName"),
                location=item.get("locationName") or item.get("geoLocationName"),
                description=item.get("description"),
                date_range=_date_range(item.get("dateRange")),
            )
        )
    return entries


def _map_education(included: list[dict]) -> list[EducationEntry]:
    schools_by_urn = {s.get("entityUrn"): s for s in _by_type(included, "School")}
    companies_by_urn = {c.get("entityUrn"): c for c in _by_type(included, "Company")}

    entries = []
    for item in _by_type(included, "Education"):
        school_urn = item.get("schoolUrn")
        school = schools_by_urn.get(school_urn) or companies_by_urn.get(school_urn) or {}
        entries.append(
            EducationEntry(
                school=item.get("schoolName") or school.get("name"),
                degree=item.get("degreeName"),
                field_of_study=item.get("fieldOfStudy"),
                date_range=_date_range(item.get("dateRange")),
            )
        )
    return entries


def _map_certifications(included: list[dict]) -> list[CertificationEntry]:
    entries = []
    for item in _by_type(included, "Certification"):
        entries.append(
            CertificationEntry(
                name=item.get("name"),
                authority=item.get("authority"),
                time_period=_format_date((item.get("timePeriod") or {}).get("start")),
                url=item.get("url"),
            )
        )
    return entries


def _map_languages(included: list[dict]) -> list[LanguageEntry]:
    entries = []
    for item in _by_type(included, "Language"):
        entries.append(LanguageEntry(name=item.get("name"), proficiency=item.get("proficiency")))
    return entries


def _map_skills(included: list[dict]) -> list[str]:
    return [s["name"] for s in _by_type(included, "Skill") if s.get("name")]


def _location(profile: dict, included: list[dict]) -> Optional[str]:
    """
    Unlike the legacy `profileView` endpoint, this decoration doesn't
    project a human-readable name onto the profile's `Geo` entity (only
    `entityUrn`/`countryCode`) — resolving it would require an extra,
    unverified lookup call. As a best-effort fallback, use the most
    recently listed position's location name, which is projected directly.
    """
    positions = _by_type(included, "Position")
    for item in positions:
        name = item.get("locationName") or item.get("geoLocationName")
        if name:
            return name
    return None


def map_profile(slug: str, raw: dict) -> ProfileResponse:
    """Translate a raw `/identity/dash/profiles` payload into our ProfileResponse."""
    included = raw.get("included", [])
    root_urn = raw.get("root_urn")

    profile = next(
        (e for e in _by_type(included, "Profile") if e.get("entityUrn") == root_urn),
        None,
    )
    if profile is None:
        # Fall back to the first Profile entity if the root urn didn't match
        # exactly (e.g. urn formatting differences between response fields).
        profiles = _by_type(included, "Profile")
        profile = profiles[0] if profiles else {}

    name = " ".join(part for part in (profile.get("firstName"), profile.get("lastName")) if part) or None

    return ProfileResponse(
        slug=slug,
        name=name,
        headline=profile.get("headline"),
        location=_location(profile, included),
        about=profile.get("summary"),
        experience=_map_experience(included),
        education=_map_education(included),
        skills=_map_skills(included),
        certifications=_map_certifications(included),
        languages=_map_languages(included),
        images=ProfileImages(
            profile_picture=_picture_url(profile.get("profilePicture")),
            background_picture=_picture_url(profile.get("backgroundPicture")),
        ),
    )
