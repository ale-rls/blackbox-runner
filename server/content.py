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

RoundType = Literal["majority", "minority", "correct_zone"]


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
    options: list[AnswerOption]

    @model_validator(mode="after")
    def _check_options(self) -> "RoundContent":
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


def load_show(path: str | Path, *, valid_zone_ids: Optional[set[str]] = None) -> ShowContent:
    raw = yaml.safe_load(Path(path).read_text())
    try:
        show = ShowContent.model_validate(raw)
    except Exception as exc:
        raise ContentError(f"invalid show content at {path}: {exc}") from exc

    if valid_zone_ids is not None:
        for round_ in show.rounds:
            for option in round_.options:
                if option.zone not in valid_zone_ids:
                    raise ContentError(
                        f"round {round_.id!r} option {option.label!r} references unknown "
                        f"zone {option.zone!r}; known zones: {sorted(valid_zone_ids)}"
                    )
    return show
