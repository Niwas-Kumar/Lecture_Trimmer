"""Data models shared across pipeline stages."""
from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class Segment(BaseModel):
    """A stretch of transcribed speech."""
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    language: str
    duration: float
    segments: List[Segment]


class Label(str, Enum):
    CORE_TEACHING = "core_teaching"              # explaining a concept, theory, derivation
    WORKED_EXAMPLE = "worked_example"            # solving a problem / numerical on the board
    DOUBT_SOLVING = "doubt_solving"              # answering a student's subject question
    LOW_DENSITY = "low_density_teaching"         # on topic but adds nothing new: repetition, transitions, filler
    REVISION_PREVIOUS = "revision_of_previous"   # recapping an earlier class
    CHATTER = "chatter"                          # greetings, jokes, motivation, small talk
    ADMIN = "admin_announcement"                 # schedule, tests, homework, attendance
    TECHNICAL_ISSUE = "technical_issue"          # "is my mic working", screen-share problems
    SILENCE_BREAK = "silence_break"              # break, waiting, no speech


class UnitLabel(BaseModel):
    """Classification of one transcript unit, returned by Claude."""
    unit_id: int = Field(description="The id of the unit being labelled")
    label: Label
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="One short sentence explaining the label")


class WindowClassification(BaseModel):
    """Claude's answer for one window of units."""
    labels: List[UnitLabel]


class Unit(BaseModel):
    """A ~45 s block of transcript that receives exactly one label."""
    id: int
    start: float
    end: float
    text: str


class KeepRange(BaseModel):
    start: float
    end: float
