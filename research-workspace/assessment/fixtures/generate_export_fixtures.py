"""Reproduce invented export fixtures via the real pure Outfitter validator."""
import argparse
import copy
import importlib.util
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from research_workspace import assessment as a
from research_workspace.assessment_estimation import estimate
from research_workspace.allocation_export import export_bytes, PROFILES


def generate(output, outfitter):
    spec = importlib.util.spec_from_file_location("invented", Path(__file__).with_name("generate_estimation_fixtures.py"))
    invented = importlib.util.module_from_spec(spec); spec.loader.exec_module(invented)
    research = invented.series()
    classroom = copy.deepcopy(research)
    classroom["manifest"].update(pathway="instructional", instruction={"enrollment": 12, "simultaneous_sessions": 3,
        "assignment_count": 2, "deadline_peak_sessions": 6, "per_student_storage_bytes": 1048576})
    for receipt in classroom["receipts"]:
        for kind in ("measurement", "cleanup"):
            receipt[kind]["binding"]["manifest_sha256"] = a.manifest_digest(classroom["manifest"])
        receipt["measurement"]["cleanup_receipt_sha256"] = a.manifest_digest(receipt["cleanup"])
    output.mkdir(parents=True, exist_ok=True)
    for name, series in (("research", research), ("classroom", classroom)):
        (output / (name + "-estimate.json")).write_bytes(a.canonical_bytes(estimate(invented.scenario(), [series], outfitter)))
        (output / (name + "-manifest.json")).write_bytes(a.canonical_bytes(series["manifest"]))
    narratives = {
      "access_explore": dict(title="Synthetic Explore planning example", planned_work_summary="Explore summary calculations on invented data. No real project or performance claim is made.",
        keywords="Synthetic data, reproducibility", field_of_science="Unselected; operator review required.", supporting_grant_details="No supporting grant asserted by this synthetic fixture.",
        preferred_resource="Unselected; availability and units require review.", alternative_resource="Unselected; no hardware recommendation."),
      "access_accelerate": dict(title="Synthetic Accelerate planning example", planned_work_summary="Plan a later measured summary-method pilot using invented arithmetic only.",
        keywords="Synthetic data, reproducibility", field_of_science="Unselected; operator review required.", supporting_grant_details="No supporting grant asserted by this synthetic fixture.",
        research_objectives="This fixture demonstrates document structure only; scientific objectives must be supplied by the researcher.",
        resource_estimates="Retain the provisional counters below. Actual core, node or GPU hours, storage capacity and resource conversions are unverified.",
        computational_plan="The invented design uses three fitting sizes and one held-out size. Distribution across systems and actual performance remain untested.",
        software_special_needs="The fixture uses the reviewed Python summary recipe. Real software, scheduling and memory needs require review.",
        team_preparedness="No staffing, qualifications, account readiness or support commitment is asserted by this fixture.",
        preferred_resource="Unselected; availability and units require review.", alternative_resource="Unselected; no hardware recommendation."),
      "access_discover": dict(title="Synthetic summary-method planning", objectives="Compare summary calculations on invented tabular data.",
        resource_needs="Use the provisional arithmetic below to plan a later measured pilot.", preferred_resource="Unselected; availability and units need review.", alternative_resource="Unselected; equivalent hardware has not been tested."),
      "nairr_research_draft": dict(title="Synthetic research planning draft", research_objectives="Test repeatable summary-method calculations on invented data.",
        workload_plan="Python fitting sizes 10, 20 and 30 rows; held-out size 40 rows; three repetitions each.",
        resource_justification="Use provisional estimates only to define a later measurement exercise.", preferred_resource="Unselected; current availability unknown.", alternative_resource="Unselected; no hardware recommendation."),
      "nairr_classroom_draft": dict(title="Synthetic classroom planning draft", learning_objectives="Practice reproducible tabular summaries and explain uncertainty.",
        course_schedule="A hypothetical two-assignment course; dates remain unselected.", assignment_plan="Twelve synthetic participants, three assumed simultaneous sessions, six at deadline peak.",
        access_and_support="Institutional access, teaching support and accessibility review remain pending.", preferred_resource="Unselected; current Classroom instructions unverified.", alternative_resource="Unselected; independent resource review required.")}
    for profile in PROFILES:
        (output / (profile + "-narrative.json")).write_bytes(export_bytes({"schema": "waypoint.allocation-narrative/v1", "fields": narratives[profile]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outfitter", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(); generate(Path(args.output), args.outfitter)
