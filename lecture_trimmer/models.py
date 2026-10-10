"""Data models shared across pipeline stages."""
from enum import Enum
from typing import List

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema


class Segment(BaseModel):
    """A stretch of transcribed speech."""
    start: float
    end: float
    text: str


class Word(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str


class Transcript(BaseModel):
    language: str
    duration: float
    segments: List[Segment]
    words: List[Word] = Field(default_factory=list)


class Label(str, Enum):
    CORE_TEACHING = "core_teaching"              # explaining a concept, theory, derivation
    WORKED_EXAMPLE = "worked_example"            # solving a problem / numerical on the board
    DOUBT_SOLVING = "doubt_solving"              # doubt tied to a specific exercise
    LOW_DENSITY = "low_density_teaching"         # on topic but adds nothing new: repetition, transitions, filler
    REVISION_PREVIOUS = "revision_of_previous"   # recapping an earlier class
    CHATTER = "chatter"                          # greetings, jokes, motivation, small talk
    ADMIN = "admin_announcement"                 # schedule, tests, homework, attendance
    TECHNICAL_ISSUE = "technical_issue"          # "is my mic working", screen-share problems
    SILENCE_BREAK = "silence_break"              # break, waiting, no speech


class Retention(str, Enum):
    ACADEMIC = "academic"
    REPETITION = "repetition"
    NONACADEMIC = "nonacademic"
    LOGISTICS = "logistics"
    WAITING = "waiting"
    UNINTELLIGIBLE = "unintelligible"
    FILLER = "filler"


class UnitLabel(BaseModel):
    """Classification of one transcript unit, returned by Claude."""
    unit_id: int = Field(description="The id of the unit being labelled")
    label: Label
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="One short sentence explaining the label")
    retention: Retention = Retention.ACADEMIC
    retention_reason: str = ""
    mixed_content: bool = False
    activity_id: int | None = None
    activity_title: str = ""
    review_required: bool = False

    @property
    def keep(self):
        return self.retention == Retention.ACADEMIC


class RetentionDecision(BaseModel):
    unit_id: int = Field(description="Exact bracketed transcript unit ID, NOT seconds")
    retention: Retention
    reason: str = Field(description="2-8 words about THIS unit's content only")
    mixed_content: bool = Field(description="This unit itself contains both essential academic content and removable speech")


class RetentionReview(BaseModel):
    decisions: List[RetentionDecision]


class WindowClassification(BaseModel):
    """Claude's answer for one window of units."""
    labels: List[UnitLabel]


class ClassificationSpan(BaseModel):
    first_id: int = Field(description="Inclusive unit_id from the transcript, not a timestamp or question number")
    last_id: int = Field(description="Inclusive last unit_id in this span, not a timestamp or question number")
    label: Label
    confidence: float = Field(ge=0, le=1)
    reason: str


class SpanClassification(BaseModel):
    spans: List[ClassificationSpan]


class ProblemEpisode(BaseModel):
    first_id: int = Field(description="Inclusive bracketed transcript unit ID, NOT a timestamp")
    last_id: int = Field(description="Inclusive last unit ID, at least first_id; do not overlap another episode")
    task: str
    evidence: str


class EpisodeMap(BaseModel):
    episodes: List[ProblemEpisode]


class Unit(BaseModel):
    """Speech-boundary-aligned text that receives exactly one label."""
    id: int
    start: float
    end: float
    text: str


class KeepRange(BaseModel):
    start: float
    end: float


class ActivityKind(str, Enum):
    THEORY = "theory"
    QUESTIONS = "questions"
    NONACADEMIC = "nonacademic"
    UNCERTAIN = "uncertain"


class ActivityRole(str, Enum):
    """What observably happened. The program, not the model, maps roles to videos."""
    CONCEPT = "concept"                # explains, defines, derives, proves or narrates subject matter
    ILLUSTRATION = "illustration"      # example used to explain a concept; no answer is asked of students
    TASK = "task"                      # a posed exercise with a definite answer: statement, attempt, solution, answer
    TASK_DOUBT = "task_doubt"          # a student's doubt about one specific exercise, and its answer
    CONCEPT_DOUBT = "concept_doubt"    # a standalone conceptual doubt, and its answer
    RECAP = "recap"                    # summary or revision of concepts
    OFF_TOPIC = "off_topic"            # greetings, banter, motivation, unrelated stories or songs
    LOGISTICS = "logistics"            # schedule, homework submission, polls, platform, attendance
    TECHNICAL = "technical"            # microphone, screen or connection trouble
    WAITING = "waiting"                # waiting for students or chat, countdowns, idle time
    UNCLEAR = "unclear"                # purpose cannot be determined from the evidence


KIND_BY_ROLE = {
    ActivityRole.CONCEPT: ActivityKind.THEORY,
    ActivityRole.ILLUSTRATION: ActivityKind.THEORY,
    ActivityRole.RECAP: ActivityKind.THEORY,
    ActivityRole.CONCEPT_DOUBT: ActivityKind.THEORY,
    ActivityRole.TASK: ActivityKind.QUESTIONS,
    ActivityRole.TASK_DOUBT: ActivityKind.QUESTIONS,
    ActivityRole.OFF_TOPIC: ActivityKind.NONACADEMIC,
    ActivityRole.LOGISTICS: ActivityKind.NONACADEMIC,
    ActivityRole.TECHNICAL: ActivityKind.NONACADEMIC,
    ActivityRole.WAITING: ActivityKind.NONACADEMIC,
    ActivityRole.UNCLEAR: ActivityKind.UNCERTAIN,
}


class TeachingActivity(BaseModel):
    first_id: int = Field(description="First inclusive transcript unit_id, NOT seconds")
    last_id: int = Field(description="Last inclusive transcript unit_id, NOT seconds")
    role: ActivityRole | None = Field(default=None, description="Required: what observably happens in this passage")
    evidence: str = Field(default="", description=(
        "For task or task_doubt: the task statement quoted word-for-word from the transcript. "
        "Otherwise a short quote that shows the role"))
    # Hidden from the model's schema: the program derives the video category from the role.
    kind: SkipJsonSchema[ActivityKind] = ActivityKind.UNCERTAIN
    title: str = Field(description="Specific learning goal or unrelated activity")
    summary: str = Field(description="What this complete activity explains or solves")
    depends_on: List[int] = Field(default_factory=list, description="First unit IDs of earlier academic activities strictly necessary to understand this one")
    needs_visual_review: bool = Field(default=False, description="Speech alone is ambiguous, garbled, or relies on silent board work")
    continues_previous: bool = Field(default=False, description="Only the first activity in a window: continues the final activity from the preceding window")

    @model_validator(mode="after")
    def _kind_from_role(self):
        # An explicit kind (saved analysis, visual review, tests) is kept as given.
        if self.role is not None and "kind" not in self.model_fields_set:
            self.kind = KIND_BY_ROLE[self.role]
        return self


class LessonMap(BaseModel):
    activities: List[TeachingActivity]


class CutCategory(str, Enum):
    """The only reasons a passage may be removed; values match Retention."""
    NONACADEMIC = "nonacademic"
    LOGISTICS = "logistics"
    WAITING = "waiting"
    FILLER = "filler"
    REPETITION = "repetition"


class PassageCut(BaseModel):
    first_id: int
    last_id: int
    reason: str
    # Restricted so the model cannot offer a reason the validator would reject
    # (e.g. 'unintelligible' garbled speech, which may be real teaching).
    category: CutCategory
    standalone: bool = Field(description="Both kept sides remain complete and understandable after removing this entire passage")


class ActivityEdit(BaseModel):
    activity_id: int = Field(description="First unit ID of this activity")
    cuts: List[PassageCut] = Field(description="Stage 1 unrelated passages and stage 2 tightening cuts")
    essential_ids: List[int] = Field(description="IDs carrying setup, essential reasoning, conditions and conclusion; these must remain")
    review_required: bool
    review_reason: str
    subactivities: List[TeachingActivity] = Field(default_factory=list, description=(
        "Optional corrected complete learning activities covering this parent exactly. "
        "Use only for real goal transitions or a full-range category correction; empty preserves the map."))


class LessonEdits(BaseModel):
    activities: List[ActivityEdit]


class VisualActivityReview(BaseModel):
    activity_id: int
    kind: ActivityKind
    reason: str
    # Frames can rescue a suspected nonacademic passage, but cannot prove spoken
    # content is irrelevant. Visual review never introduces a new deletion.
    preserve: bool


class VisualReviews(BaseModel):
    reviews: List[VisualActivityReview]
