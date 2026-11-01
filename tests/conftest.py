import pytest

from testcase_agent.documents import Document
from testcase_agent.generation import make_suite
from testcase_agent.models import Analysis, CaseBatch, DraftCase, Evidence, Requirement, SourceRecord


def requirement(rid="FR-01", description="Retain rows whose Status is exactly Active", related=None):
    return Requirement(
        id=rid,
        description=description,
        acceptance_criteria=[description],
        evidence=[Evidence(source="BRD.txt", location="paragraph 1", excerpt=description)],
        related_ids=related or [],
        ambiguities=[],
    )


class FakeGenerator:
    model = "deterministic-test-fixture"

    def __init__(self, requirements=None):
        self.requirements = requirements or [requirement()]
        self.generated = []

    def analyze(self, docs, prior):
        return Analysis(requirements=self.requirements, gaps=[])

    def generate(self, targets, context):
        self.generated.extend(r.id for r in targets)
        return CaseBatch(
            cases=[
                DraftCase(
                    requirement_ids=[r.id],
                    category=category,
                    description=f"{category}: {r.description}",
                    steps=["Prepare synthetic data", "Run pipeline"],
                    expected_result="The documented outcome is observed",
                    severity="High",
                    evidence=r.evidence,
                )
                for r in targets
                for category in ("Positive", "Negative", "Boundary", "Exception")
            ],
            exclusions=[],
        )


@pytest.fixture
def sample_suite():
    docs = [Document(SourceRecord(name="BRD.txt", locator="upload:BRD.txt", sha256="abc"), "Status Active")]
    return make_suite("project-1", docs, "bundle-1", FakeGenerator())
