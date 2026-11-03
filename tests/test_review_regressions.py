"""Synthetic regressions for M1 commit publication and evidence authority."""

from dataclasses import FrozenInstanceError
import inspect
import sys
import unittest
from uuid import UUID

from genesis.core import GenesisCore, UserInput
from genesis.growth import GrowthCandidate
from genesis.identity import CompanionIdentity
from genesis.memory import EvidenceOrigin, EvidenceReference, TrustedEvidenceRegistry
from genesis.models.fake import FakeModel


def uid(number: int) -> UUID:
    return UUID(int=number)


class CommitRegressionTests(unittest.TestCase):
    def test_interrupt_before_or_after_publication_has_consistent_retry(self) -> None:
        source, start = inspect.getsourcelines(GenesisCore.send)
        targets = {
            "before": start + next(i for i, line in enumerate(source)
                                   if "self._sessions[session_id] = new_session" in line),
            "after": start + next(i for i, line in enumerate(source)
                                  if line.strip() == "return turn"),
        }
        for phase, target in targets.items():
            with self.subTest(phase=phase):
                model = FakeModel(["synthetic prior", "synthetic interrupted", "synthetic retry"])
                core = GenesisCore(CompanionIdentity(uid(1)), model)
                session = core.create_session(uid(2))
                core.send(session.session_id, UserInput(uid(3), "synthetic prior input"))
                before = core.get_session(session.session_id)
                user_input = UserInput(uid(4), "synthetic interrupted input")

                def interrupt(frame, event, arg):
                    if (frame.f_code is GenesisCore.send.__code__ and event == "line"
                            and frame.f_lineno == target):
                        raise KeyboardInterrupt("synthetic commit interruption")
                    return interrupt

                previous_trace = sys.gettrace()
                try:
                    sys.settrace(interrupt)
                    with self.assertRaises(KeyboardInterrupt):
                        core.send(session.session_id, user_input)
                finally:
                    sys.settrace(previous_trace)
                interrupted = core.get_session(session.session_id)
                if phase == "before":
                    self.assertEqual(interrupted, before)
                else:
                    self.assertEqual(len(interrupted.turns), 2)
                reply = core.send(session.session_id, user_input)
                replay = core.send(session.session_id, user_input)
                final = core.get_session(session.session_id)
                self.assertEqual([turn.user_input.input_id for turn in final.turns], [uid(3), uid(4)])
                self.assertIs(reply, replay)
                self.assertEqual(len(model.requests), 3 if phase == "before" else 2)
                self.assertEqual(reply.assistant.content,
                                 "synthetic retry" if phase == "before" else "synthetic interrupted")
                self.assertEqual(len(before.turns), 1)


class EvidenceRegressionTests(unittest.TestCase):
    def test_model_root_cannot_be_laundered_through_tool_echo(self) -> None:
        root = EvidenceReference(uid(1), EvidenceOrigin.MODEL_OUTPUT, uid(1))
        echo = EvidenceReference(uid(2), EvidenceOrigin.TOOL_RESULT, uid(1))
        candidate = GrowthCandidate(uid(3), "synthetic model self-report", (root, echo))
        self.assertEqual(candidate.independent_evidence_ids(TrustedEvidenceRegistry((root, echo))),
                         frozenset())

    def test_model_summary_preserves_verified_user_root(self) -> None:
        root = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        summary = EvidenceReference(uid(2), EvidenceOrigin.MODEL_OUTPUT, uid(1))
        candidate = GrowthCandidate(uid(3), "synthetic summarized user evidence", (summary,))
        self.assertEqual(candidate.independent_evidence_ids(TrustedEvidenceRegistry((root, summary))),
                         frozenset({uid(1)}))

    def test_verified_external_roots_are_deduplicated(self) -> None:
        user = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        tool = EvidenceReference(uid(2), EvidenceOrigin.TOOL_RESULT, uid(2))
        derived = EvidenceReference(uid(3), EvidenceOrigin.MODEL_OUTPUT, uid(1))
        candidate = GrowthCandidate(uid(4), "synthetic distinct observations", (user, tool, derived))
        self.assertEqual(candidate.independent_evidence_ids(TrustedEvidenceRegistry((user, tool, derived))),
                         frozenset({uid(1), uid(2)}))

    def test_reference_without_attestation_is_not_trusted(self) -> None:
        claimed = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        candidate = GrowthCandidate(uid(2), "synthetic unverified claim", (claimed,))
        self.assertEqual(candidate.independent_evidence_ids(TrustedEvidenceRegistry()), frozenset())

    def test_registered_derived_event_with_missing_or_nonroot_target_is_unconfirmed(self) -> None:
        derived = EvidenceReference(uid(2), EvidenceOrigin.TOOL_RESULT, uid(1))
        nonroot = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(99))
        for events in ((derived,), (derived, nonroot)):
            with self.subTest(events=events):
                candidate = GrowthCandidate(uid(3), "synthetic incomplete provenance", (derived,))
                self.assertEqual(candidate.independent_evidence_ids(TrustedEvidenceRegistry(events)),
                                 frozenset())

    def test_forged_origin_or_root_cannot_override_host_attestation(self) -> None:
        user = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        model = EvidenceReference(uid(2), EvidenceOrigin.MODEL_OUTPUT, uid(2))
        registry = TrustedEvidenceRegistry((user, model))
        for claimed in (
            EvidenceReference(uid(2), EvidenceOrigin.USER_INPUT, uid(2)),
            EvidenceReference(uid(2), EvidenceOrigin.MODEL_OUTPUT, uid(1)),
            EvidenceReference(uid(99), EvidenceOrigin.USER_INPUT, uid(1)),
        ):
            with self.subTest(claimed=claimed):
                candidate = GrowthCandidate(uid(3), "synthetic forged provenance", (claimed,))
                self.assertEqual(candidate.independent_evidence_ids(registry), frozenset())

    def test_registry_copies_host_events_and_rejects_conflicting_ids(self) -> None:
        root = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        events = [root]
        registry = TrustedEvidenceRegistry(events)
        events.clear()
        candidate = GrowthCandidate(uid(2), "synthetic evidence", (root,))
        self.assertEqual(candidate.independent_evidence_ids(registry), frozenset({uid(1)}))
        with self.assertRaises(FrozenInstanceError):
            registry._events = {}
        with self.assertRaises(ValueError):
            TrustedEvidenceRegistry((root, EvidenceReference(uid(1), EvidenceOrigin.MODEL_OUTPUT, uid(1))))

    def test_candidate_cannot_supply_its_own_trust_mapping(self) -> None:
        root = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        candidate = GrowthCandidate(uid(2), "synthetic trust spoof", (root,))
        with self.assertRaises(TypeError):
            candidate.independent_evidence_ids({uid(1): EvidenceOrigin.USER_INPUT})
        with self.assertRaises(TypeError):
            GrowthCandidate(uid(2), "synthetic trust spoof", (root,), registry=TrustedEvidenceRegistry((root,)))
