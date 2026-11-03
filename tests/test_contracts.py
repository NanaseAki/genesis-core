"""Synthetic provenance and adapter contract tests; no growth algorithm."""

from dataclasses import FrozenInstanceError
import unittest
from uuid import UUID

from genesis.growth import GrowthCandidate
from genesis.identity import CompanionIdentity
from genesis.memory import (
    EvidenceOrigin, EvidenceReference, MemoryKind, MemoryRecord, TrustedEvidenceRegistry,
)
from genesis.models.contracts import Message, ModelError, ModelReply, ModelRequest, Role
from genesis.models.fake import FakeModel
from genesis.policy import Action, DefaultPolicy


def uid(number: int) -> UUID:
    return UUID(int=number)


def request() -> ModelRequest:
    return ModelRequest(uid(1), uid(2), uid(3), (Message(Role.USER, "synthetic hello"),))


class ContractTests(unittest.TestCase):
    def test_default_fake_is_deterministic_without_script(self) -> None:
        first, second = FakeModel(), FakeModel()
        self.assertEqual(first.generate(request()), second.generate(request()))
        self.assertEqual(first.generate(request()).content, "Fake response: synthetic hello")

    def test_script_is_copied_and_exhaustion_is_explicit(self) -> None:
        script = ["synthetic configured"]
        model = FakeModel(script)
        script[0] = "synthetic changed outside"
        self.assertEqual(model.generate(request()).content, "synthetic configured")
        with self.assertRaisesRegex(ModelError, "fake_script_exhausted"):
            model.generate(request())

    def test_empty_script_is_not_an_echo_fallback(self) -> None:
        with self.assertRaises(ModelError):
            FakeModel([]).generate(request())

    def test_explicit_failure_step_is_reproducible(self) -> None:
        model = FakeModel([ModelError("synthetic_failure"), "synthetic recovery"])
        with self.assertRaisesRegex(ModelError, "synthetic_failure"):
            model.generate(request())
        self.assertEqual(model.generate(request()).content, "synthetic recovery")

    def test_request_rejects_mutable_history_or_non_user_tail(self) -> None:
        with self.assertRaises(ValueError):
            ModelRequest(uid(1), uid(2), uid(3), [Message(Role.USER, "synthetic")])
        with self.assertRaises(ValueError):
            ModelRequest(uid(1), uid(2), uid(3), (Message(Role.ASSISTANT, "synthetic"),))
        with self.assertRaises(TypeError):
            ModelRequest("fake-v1", uid(2), uid(3), request().messages)

    def test_blank_reply_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            ModelReply(" \n")

    def test_identity_requires_an_explicit_uuid(self) -> None:
        with self.assertRaises(TypeError):
            CompanionIdentity("fake-v1")

    def test_observation_inference_and_candidate_have_distinct_kinds(self) -> None:
        source = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        observation = MemoryRecord(uid(2), MemoryKind.OBSERVATION, "synthetic user statement", (source,))
        inference = MemoryRecord(uid(3), MemoryKind.INFERENCE, "synthetic interpretation", (source,))
        candidate = GrowthCandidate(uid(4), "synthetic possible preference", (source,))
        self.assertEqual(len({observation.kind.value, inference.kind.value, candidate.kind}), 3)
        with self.assertRaises(FrozenInstanceError):
            inference.kind = MemoryKind.OBSERVATION

    def test_model_repetitions_never_add_independent_preference_evidence(self) -> None:
        sources = tuple(EvidenceReference(uid(index), EvidenceOrigin.MODEL_OUTPUT, uid(index))
                        for index in range(10, 15))
        candidate = GrowthCandidate(uid(99), "synthetic repeated self-report", sources)
        self.assertEqual(
            candidate.independent_evidence_ids(TrustedEvidenceRegistry(sources)), frozenset()
        )

    def test_derived_evidence_with_same_root_counts_once(self) -> None:
        sources = (
            EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1)),
            EvidenceReference(uid(2), EvidenceOrigin.TOOL_RESULT, uid(1)),
            EvidenceReference(uid(3), EvidenceOrigin.MODEL_OUTPUT, uid(3)),
            EvidenceReference(uid(4), EvidenceOrigin.USER_INPUT, uid(4)),
        )
        candidate = GrowthCandidate(uid(99), "synthetic proposal", sources)
        self.assertEqual(
            candidate.independent_evidence_ids(TrustedEvidenceRegistry(sources)),
            frozenset({uid(1), uid(4)}),
        )

    def test_provenance_is_required_and_must_be_immutable(self) -> None:
        with self.assertRaises(ValueError):
            MemoryRecord(uid(2), MemoryKind.INFERENCE, "synthetic unsupported", ())
        with self.assertRaises(ValueError):
            GrowthCandidate(uid(3), "synthetic unsupported", ())
        source = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        with self.assertRaises(ValueError):
            GrowthCandidate(uid(3), "synthetic mutable", [source])
        with self.assertRaises(TypeError):
            EvidenceReference(uid(1), "user_input", uid(1))

    def test_default_policy_only_allows_reply_and_has_no_mutable_grants(self) -> None:
        policy = DefaultPolicy()
        for action in Action:
            with self.subTest(action=action):
                self.assertEqual(policy.authorize(action).allowed, action is Action.RESPOND)
        self.assertFalse(policy.authorize("unknown_action").allowed)
        with self.assertRaises(FrozenInstanceError):
            policy.grants = {Action.CLOUD_INFERENCE}
