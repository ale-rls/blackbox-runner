"""Loads and validates content/show.yaml — rounds, questions, and the
zone->answer mapping (docs/architecture.md §3).

Every answer option must reference a zone id that exists in TrackingBox's
/api/zones (fetched at startup), so a content typo fails fast at load time
instead of silently making an option unanswerable mid-show.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, model_validator

RoundType = Literal["majority", "minority", "correct_zone", "narration"]

# How the player page renders a question step. "choice" is the classic
# option list; the others mirror the physical floor markings (pink scale
# line, cross axes, quadrant fields, concentric rings).
Form = Literal["choice", "scale", "scale3", "cross", "quadrants", "rings"]

# form_labels keys each form requires so the player page always has its
# pole/axis captions. "choice" and "quadrants" label via options instead.
FORM_REQUIRED_LABELS: dict[str, tuple[str, ...]] = {
    "choice": (),
    "scale": ("left", "right"),
    "scale3": ("left", "middle", "right"),
    "cross": ("x_left", "x_right", "y_top", "y_bottom"),
    "quadrants": (),
    "rings": ("center", "edge"),
}


class ContentError(ValueError):
    pass


class AnswerOption(BaseModel):
    zone: str
    label: str
    correct: bool = False


class RoundContent(BaseModel):
    id: str
    question: str
    type: RoundType = "majority"
    duration_s: float = 20.0
    grace_s: float = 5.0
    points: int = 10
    options: list[AnswerOption] = []
    # Narration/question text read to the player (displayed + spoken).
    text: Optional[str] = None
    # mp3 filename inside the audio dir, served at /audio/{audio}.
    audio: Optional[str] = None
    form: Form = "choice"
    form_labels: dict[str, str] = {}

    @model_validator(mode="after")
    def _check_options(self) -> "RoundContent":
        if self.type == "narration":
            if self.options:
                raise ValueError(f"narration round {self.id!r} must not have options")
            return self
        if len(self.options) < 2:
            raise ValueError(f"round {self.id!r} needs at least 2 options")
        zones = [o.zone for o in self.options]
        if len(set(zones)) != len(zones):
            raise ValueError(f"round {self.id!r} has duplicate zones in options")
        if self.type == "correct_zone":
            correct = [o for o in self.options if o.correct]
            if len(correct) != 1:
                raise ValueError(
                    f"round {self.id!r} is type correct_zone but doesn't have exactly one "
                    f"option marked correct: true"
                )
        missing = [k for k in FORM_REQUIRED_LABELS[self.form] if not self.form_labels.get(k)]
        if missing:
            raise ValueError(
                f"round {self.id!r} form {self.form!r} is missing form_labels: {missing}"
            )
        return self


class ShowContent(BaseModel):
    version: str = "1"
    rounds: list[RoundContent]

    @model_validator(mode="after")
    def _check_round_ids(self) -> "ShowContent":
        ids = [r.id for r in self.rounds]
        if len(set(ids)) != len(ids):
            raise ValueError("round ids must be unique")
        return self


def validate_show(
    raw: object, *, valid_zone_ids: Optional[set[str]] = None, source: str = "show content"
) -> ShowContent:
    try:
        show = ShowContent.model_validate(raw)
    except Exception as exc:
        raise ContentError(f"invalid {source}: {exc}") from exc

    if valid_zone_ids is not None:
        for round_ in show.rounds:
            for option in round_.options:
                if option.zone not in valid_zone_ids:
                    raise ContentError(
                        f"round {round_.id!r} option {option.label!r} references unknown "
                        f"zone {option.zone!r}; known zones: {sorted(valid_zone_ids)}"
                    )
    return show


def load_show(path: str | Path, *, valid_zone_ids: Optional[set[str]] = None) -> ShowContent:
    raw = yaml.safe_load(Path(path).read_text())
    return validate_show(raw, valid_zone_ids=valid_zone_ids, source=f"show content at {path}")
