from typing import Literal

from pydantic import BaseModel, Field, model_validator

Category = Literal["Positive", "Negative", "Boundary", "Exception"]
Status = Literal["Not Run", "Pass", "Fail", "Blocked", "Not Applicable"]
CATEGORIES = ("Positive", "Negative", "Boundary", "Exception")


class Evidence(BaseModel):
    source: str
    location: str
    excerpt: str


class Requirement(BaseModel):
    id: str
    description: str
    acceptance_criteria: list[str]
    evidence: list[Evidence]
    related_ids: list[str]
    ambiguities: list[str]


class Analysis(BaseModel):
    requirements: list[Requirement]
    gaps: list[str]

    @model_validator(mode="after")
    def validate_ids(self):
        ids = [r.id for r in self.requirements]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Analysis must contain requirements with unique IDs")
        for r in self.requirements:
            if not r.id.strip() or not r.evidence:
                raise ValueError("Every requirement needs an ID and source evidence")
            if set(r.related_ids) - set(ids):
                raise ValueError("Related requirement IDs must exist")
        return self


class DraftCase(BaseModel):
    requirement_ids: list[str]
    category: Category
    description: str
    steps: list[str]
    expected_result: str
    severity: Literal["Critical", "High", "Medium", "Low"]
    evidence: list[Evidence]


class Exclusion(BaseModel):
    requirement_id: str
    category: Category
    reason: str


class CaseBatch(BaseModel):
    cases: list[DraftCase]
    exclusions: list[Exclusion]


class TestCase(DraftCase):
    id: str
    status: Status = "Not Run"
    change: str = "Added"


class SourceRecord(BaseModel):
    name: str
    locator: str
    sha256: str
    notes: list[str] = Field(default_factory=list)


class Suite(BaseModel):
    project_id: str
    version: int
    bundle_hash: str
    requirements: list[Requirement]
    cases: list[TestCase]
    exclusions: list[Exclusion]
    gaps: list[str]
    sources: list[SourceRecord]
    retired: list[TestCase] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    kind: Literal["Full", "Regression"] = "Full"
    generated_at: str = ""
    model: str = ""
