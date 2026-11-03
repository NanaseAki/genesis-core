"""Synthetic interaction scenarios: state, idempotency and failure semantics."""

from dataclasses import FrozenInstanceError
import unittest
from uuid import UUID

from genesis.core import (
    DuplicateInputError, GenesisCore, PolicyDeniedError, SessionExistsError,
    SessionNotFoundError, TurnGenerationError, UserInput,
)
from genesis.identity import CompanionIdentity
from genesis.models.contracts import ModelError, ModelReply, Role
from genesis.models.fake import FakeModel
from genesis.policy import PolicyDecision


def uid(number: int) -> UUID:
    return UUID(int=number)


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = CompanionIdentity(uid(1))
        self.model = FakeModel([
            "synthetic reply one", "synthetic reply two", "synthetic isolated reply"
        ])
        self.core = GenesisCore(self.identity, self.model)
        self.session = self.core.create_session(uid(2))

    def test_unicode_input_and_reply_keep_original_text(self) -> None:
        original = "  合成测试：你好 🌱  "
        turn = self.core.send(uid(2), UserInput(uid(3), original))
        self.assertEqual(turn.assistant.content, "synthetic reply one")
        self.assertEqual(turn.assistant.role, Role.ASSISTANT)
        self.assertEqual(self.model.requests[0].messages[0].content, original)
        self.assertEqual(self.core.get_session(uid(2)).messages[0].content, original)

    def test_next_request_receives_only_its_session_history(self) -> None:
        self.core.send(uid(2), UserInput(uid(3), "synthetic first"))
        self.core.send(uid(2), UserInput(uid(4), "synthetic second"))
        self.assertEqual([message.role for message in self.model.requests[1].messages],
                         [Role.USER, Role.ASSISTANT, Role.USER])
        other = self.core.create_session(uid(5))
        self.core.send(other.session_id, UserInput(uid(6), "synthetic isolated"))
        self.assertEqual(len(self.model.requests[2].messages), 1)
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 2)
        self.assertEqual(len(self.core.get_session(uid(5)).turns), 1)

    def test_completed_retry_returns_original_without_new_generation(self) -> None:
        user_input = UserInput(uid(3), "synthetic retry")
        first = self.core.send(uid(2), user_input)
        self.core.send(uid(2), UserInput(uid(4), "synthetic later input"))
        replay = self.core.send(uid(2), user_input)
        self.assertIs(first, replay)
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 2)

    def test_same_id_with_different_content_is_rejected(self) -> None:
        self.core.send(uid(2), UserInput(uid(3), "synthetic original"))
        before = self.core.get_session(uid(2))
        with self.assertRaises(DuplicateInputError):
            self.core.send(uid(2), UserInput(uid(3), "synthetic changed"))
        self.assertEqual(self.core.get_session(uid(2)), before)
        self.assertEqual(len(self.model.requests), 1)

    def test_same_text_with_new_id_is_a_new_user_event(self) -> None:
        self.core.send(uid(2), UserInput(uid(3), "synthetic same text"))
        self.core.send(uid(2), UserInput(uid(4), "synthetic same text"))
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 2)
        self.assertEqual(len(self.model.requests), 2)

    def test_input_id_scope_is_one_session(self) -> None:
        self.core.create_session(uid(5))
        self.core.send(uid(2), UserInput(uid(3), "synthetic A"))
        self.core.send(uid(5), UserInput(uid(3), "synthetic B"))
        self.assertEqual(len(self.core.get_session(uid(5)).turns), 1)

    def test_expected_failure_keeps_history_and_allows_explicit_retry(self) -> None:
        failure = ModelError("synthetic_model_unavailable")
        model = FakeModel(["synthetic prior reply", failure, "synthetic recovered"])
        core = GenesisCore(self.identity, model)
        core.create_session(uid(2))
        core.send(uid(2), UserInput(uid(3), "synthetic prior input"))
        before = core.get_session(uid(2))
        user_input = UserInput(uid(4), "synthetic retry input")
        with self.assertRaises(TurnGenerationError) as caught:
            core.send(uid(2), user_input)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(core.get_session(uid(2)), before)
        self.assertEqual(core.send(uid(2), user_input).assistant.content, "synthetic recovered")
        self.assertEqual(len(core.get_session(uid(2)).turns), 2)

    def test_unexpected_model_error_propagates_without_state_changes(self) -> None:
        error = RuntimeError("synthetic programming defect")

        class BrokenModel:
            model_id = "synthetic-broken"

            def generate(self, request):
                raise error

        core = GenesisCore(self.identity, BrokenModel())
        before = core.create_session(uid(2))
        with self.assertRaises(RuntimeError) as caught:
            core.send(uid(2), UserInput(uid(3), "synthetic input"))
        self.assertIs(caught.exception, error)
        self.assertEqual(core.get_session(uid(2)), before)

    def test_invalid_backend_result_is_not_committed(self) -> None:
        class InvalidModel:
            model_id = "synthetic-invalid"

            def generate(self, request):
                return None

        core = GenesisCore(self.identity, InvalidModel())
        before = core.create_session(uid(2))
        with self.assertRaises(TurnGenerationError):
            core.send(uid(2), UserInput(uid(3), "synthetic input"))
        self.assertEqual(core.get_session(uid(2)), before)

    def test_policy_denial_prevents_model_call_and_state_change(self) -> None:
        class DenyPolicy:
            def authorize(self, action):
                return PolicyDecision(False, "synthetic denial")

        core = GenesisCore(self.identity, self.model, DenyPolicy())
        before = core.create_session(uid(2))
        with self.assertRaises(PolicyDeniedError):
            core.send(uid(2), UserInput(uid(3), "synthetic input"))
        self.assertEqual(self.model.requests, ())
        self.assertEqual(core.get_session(uid(2)), before)

    def test_policy_error_propagates_without_model_call(self) -> None:
        error = RuntimeError("synthetic policy defect")

        class BrokenPolicy:
            def authorize(self, action):
                raise error

        core = GenesisCore(self.identity, self.model, BrokenPolicy())
        before = core.create_session(uid(2))
        with self.assertRaises(RuntimeError) as caught:
            core.send(uid(2), UserInput(uid(3), "synthetic input"))
        self.assertIs(caught.exception, error)
        self.assertEqual(self.model.requests, ())
        self.assertEqual(core.get_session(uid(2)), before)

    def test_model_receives_immutable_values_and_no_services(self) -> None:
        self.core.send(uid(2), UserInput(uid(3), "synthetic input"))
        request = self.model.requests[0]
        with self.assertRaises(FrozenInstanceError):
            request.companion_id = uid(99)
        with self.assertRaises(FrozenInstanceError):
            request.messages[0].content = "synthetic overwrite"
        self.assertEqual(set(vars(request)),
                         {"companion_id", "session_id", "input_id", "messages"})

    def test_snapshots_cannot_rewrite_active_history(self) -> None:
        empty = self.core.get_session(uid(2))
        self.core.send(uid(2), UserInput(uid(3), "synthetic input"))
        snapshot = self.core.get_session(uid(2))
        self.assertEqual(empty.turns, ())
        with self.assertRaises(FrozenInstanceError):
            snapshot.turns = ()
        with self.assertRaises(FrozenInstanceError):
            snapshot.turns[0].user_input.content = "synthetic overwrite"
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 1)

    def test_unknown_session_does_not_create_one_or_call_model(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.core.send(uid(99), UserInput(uid(3), "synthetic input"))
        self.assertEqual(self.model.requests, ())
        with self.assertRaises(SessionNotFoundError):
            self.core.get_session(uid(99))

    def test_duplicate_session_creation_does_not_clear_history(self) -> None:
        self.core.send(uid(2), UserInput(uid(3), "synthetic input"))
        before = self.core.get_session(uid(2))
        with self.assertRaises(SessionExistsError):
            self.core.create_session(uid(2))
        self.assertEqual(self.core.get_session(uid(2)), before)

    def test_blank_input_is_rejected_before_generation(self) -> None:
        with self.assertRaises(ValueError):
            self.core.send(uid(2), UserInput(uid(3), " \n\t "))
        self.assertEqual(self.model.requests, ())
        self.assertEqual(self.core.get_session(uid(2)), self.session)

    def test_identity_is_independent_of_selected_model(self) -> None:
        class OtherModel:
            model_id = "synthetic-other-model"

            def generate(self, request):
                return ModelReply("synthetic alternative")

        other = GenesisCore(self.identity, OtherModel())
        other_session = other.create_session()
        turn = other.send(other_session.session_id, UserInput(uid(3), "synthetic input"))
        self.assertNotEqual(turn.model_id, self.model.model_id)
        self.assertEqual(other_session.companion_id, self.session.companion_id)
        self.assertEqual(other.identity, self.identity)
