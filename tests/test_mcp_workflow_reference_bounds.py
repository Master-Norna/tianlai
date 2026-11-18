"""The MCP boundary must never be narrower than the core contract it forwards.

A composition map freezes only when every phase's question count fits the
answer budget, and the budget is derived from the map node ceiling.  The MCP
schema used to cap answers with a literal 128, so a map the core had just
frozen could demand up to 320 answers that the transport then refused before
the core ever saw them.  These tests pin the two together: the boundary that
carries the work must accept exactly what the core admits.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tianlai.composition_map import MAX_NODES
from tianlai.creative_workflow import MAX_REVIEW_QUESTION_ANSWERS
from tianlai.mcp_server import (
    _WorkflowCompositionMapNodes,
    _WorkflowDerivationNodeIds,
    _WorkflowDerivationQuestionIds,
    _WorkflowReviewQuestionAnswers,
)


def _question_id(index: int) -> str:
    return f"question-{index:020x}"


def _answer(index: int) -> dict[str, object]:
    return {
        "question_id": _question_id(index),
        "answer": "answered from the render receipt",
        "claim_ids": [],
        "node_ids": [],
        "event_ids": [],
    }


def test_review_answers_accept_the_core_budget() -> None:
    adapter = TypeAdapter(_WorkflowReviewQuestionAnswers)
    assert adapter.json_schema()["maxItems"] == MAX_REVIEW_QUESTION_ANSWERS
    answers = [_answer(index) for index in range(MAX_REVIEW_QUESTION_ANSWERS)]
    assert adapter.validate_python(answers) == answers


def test_review_answers_above_the_core_budget_stay_rejected() -> None:
    adapter = TypeAdapter(_WorkflowReviewQuestionAnswers)
    answers = [_answer(index) for index in range(MAX_REVIEW_QUESTION_ANSWERS + 1)]
    with pytest.raises(ValidationError):
        adapter.validate_python(answers)


def test_derivation_references_match_the_core_ceilings() -> None:
    question_ids = [_question_id(index) for index in range(MAX_REVIEW_QUESTION_ANSWERS)]
    node_ids = [f"node-{index}" for index in range(MAX_NODES)]
    questions = TypeAdapter(_WorkflowDerivationQuestionIds)
    nodes = TypeAdapter(_WorkflowDerivationNodeIds)
    assert questions.json_schema()["maxItems"] == MAX_REVIEW_QUESTION_ANSWERS
    assert nodes.json_schema()["maxItems"] == MAX_NODES
    assert questions.validate_python(question_ids) == question_ids
    assert nodes.validate_python(node_ids) == node_ids


def test_composition_map_nodes_use_the_documented_ceiling() -> None:
    adapter = TypeAdapter(_WorkflowCompositionMapNodes)
    assert adapter.json_schema()["maxItems"] == MAX_NODES
