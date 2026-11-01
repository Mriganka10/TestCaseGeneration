import pytest
from conftest import FakeGenerator, requirement

from testcase_agent.generation import make_suite, regression, validate_batch
from testcase_agent.models import Exclusion


def test_unchanged_retains_ids_and_execution(sample_suite):
    sample_suite.cases[0].status = "Pass"
    generator = FakeGenerator()
    updated = make_suite("project-1", [], "new-bundle", generator, sample_suite)
    assert generator.generated == []
    assert [c.id for c in updated.cases] == [c.id for c in sample_suite.cases]
    assert updated.cases[0].status == "Pass"
    assert updated.version == 2


def test_changed_requirement_resets_cases(sample_suite):
    sample_suite.cases[0].status = "Fail"
    updated = make_suite(
        "project-1",
        [],
        "new",
        FakeGenerator([requirement(description="Retain Active and Pending")]),
        sample_suite,
    )
    assert all(c.status == "Not Run" for c in updated.cases)
    assert set(c.id for c in updated.cases).isdisjoint(c.id for c in sample_suite.cases)
    assert len(updated.retired) == 4
    assert updated.retired[0].status == "Fail"


def test_added_removed_and_dependency_impact():
    old = make_suite(
        "p", [], "1", FakeGenerator([requirement(), requirement("FR-02", "Publish output", ["FR-01"])])
    )
    new = make_suite(
        "p",
        [],
        "2",
        FakeGenerator([requirement("FR-02", "Publish output"), requirement("FR-03", "Monitor")]),
        old,
    )
    assert len(new.retired) == 8
    assert all(c.status == "Not Run" for c in new.cases)
    assert "Removed requirement: FR-01" in new.changes


def test_missing_category_requires_explicit_exclusion():
    req = requirement()
    batch = FakeGenerator().generate([req], [req])
    batch.cases.pop()
    with pytest.raises(ValueError, match="omitted coverage"):
        validate_batch(batch, [req])
    batch.exclusions.append(
        Exclusion(
            requirement_id="FR-01", category="Exception", reason="No exception behavior specified; clarify"
        )
    )
    validate_batch(batch, [req])


def test_unknown_requirement_and_duplicate_rejected():
    req = requirement()
    batch = FakeGenerator().generate([req], [req])
    batch.cases[0].requirement_ids = ["invented"]
    with pytest.raises(ValueError, match="unknown"):
        validate_batch(batch, [req])
    batch = FakeGenerator().generate([req], [req])
    batch.cases.append(batch.cases[0])
    with pytest.raises(ValueError, match="duplicate"):
        validate_batch(batch, [req])


def test_regression_includes_related_not_unrelated():
    suite = make_suite(
        "p",
        [],
        "1",
        FakeGenerator(
            [requirement(), requirement("FR-02", "Publish", ["FR-01"]), requirement("FR-03", "Unrelated")]
        ),
    )
    suite.cases[0].status = "Fail"
    result = regression(suite)
    assert {r.id for r in result.requirements} == {"FR-01", "FR-02"}


def test_impact_cascades_through_shared_case_then_dependency():
    reqs = [requirement(), requirement("FR-02", "Publish"), requirement("FR-03", "Audit", ["FR-02"])]
    old = make_suite("p", [], "old", FakeGenerator(reqs))
    old.cases[0].requirement_ids.append("FR-02")
    reqs[0] = requirement(description="Changed selection")
    gen = FakeGenerator(reqs)
    updated = make_suite("p", [], "new", gen, old)
    assert set(gen.generated) == {"FR-01", "FR-02", "FR-03"}
    assert len(updated.retired) == 12
