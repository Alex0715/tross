"""Pydantic response models for the structured profile payload."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DateRange(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None  # None/omitted means "present"


class ExperienceEntry(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    date_range: Optional[DateRange] = None


class EducationEntry(BaseModel):
    school: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    date_range: Optional[DateRange] = None


class CertificationEntry(BaseModel):
    name: Optional[str] = None
    authority: Optional[str] = None
    time_period: Optional[str] = None
    url: Optional[str] = None


class LanguageEntry(BaseModel):
    name: Optional[str] = None
    proficiency: Optional[str] = None


class ProfileImages(BaseModel):
    profile_picture: Optional[str] = None
    background_picture: Optional[str] = None


class ProfileResponse(BaseModel):
    slug: str
    name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    about: Optional[str] = None
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[CertificationEntry] = Field(default_factory=list)
    languages: list[LanguageEntry] = Field(default_factory=list)
    images: ProfileImages = Field(default_factory=ProfileImages)


class ErrorDetail(BaseModel):
    """
    The public shape of a failure. `code` is a stable identifier clients can
    branch on; `message` is human-readable and deliberately generic —
    it never carries a cookie, token, credential, slug-level internal detail,
    or any part of a raw LinkedIn response.
    """

    code: str = Field(description="Stable, machine-readable error identifier.")
    message: str = Field(description="Human-readable, caller-safe description.")
    upstream_status: Optional[int] = Field(
        default=None,
        description="HTTP status LinkedIn returned, when the failure came from LinkedIn.",
    )


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail
