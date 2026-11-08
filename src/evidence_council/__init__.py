"""evidence-council: compare independent AI proposals and decide by objective evidence.

Stable entry points (Python):
    evidence_council.council.run_review(input_doc, config) -> output_doc
    evidence_council.council.run_stage(stage, state, config) -> state
    evidence_council.contract.validate(doc, kind) -> list[str]
    evidence_council.privacy.plan_routing(input_doc, providers, config) -> list[Route]
    evidence_council.lessons.check_candidate(lesson, store) -> CheckReport

Stable entry point (shell): the ``evidence-council`` command; see ``evidence-council --help``.
"""

__version__ = "0.2.0"
CONTRACT_VERSION = "1.0"
