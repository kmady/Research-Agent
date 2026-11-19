import json

import httpx
import pytest

from research_agent.llm import ContextOverflowError, LLMError, OllamaLLM
from research_agent.schemas import ClaimVerdict, Extraction, ResearchPlan, SubQuestionAssessment, UrlSelection

PLAN_JSON = json.dumps({"sub_questions": [{"id": "Q1", "question": "q"}], "initial_queries": ["a", "b"]})


def make_llm(responses, **kw):
    """`responses` : liste de (contenu, done_reason) renvoyés dans l'ordre ; les requêtes sont enregistrées."""
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        content, reason = responses[min(len(sent) - 1, len(responses) - 1)]
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content},
                                         "done_reason": reason, "prompt_eval_count": 100, "eval_count": 20})

    llm = OllamaLLM("http://ollama.test", "m", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)
    return llm, sent


def user(text="Planifie."):
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


def test_structured_valid_first_try_and_payload():
    llm, sent = make_llm([(PLAN_JSON, "stop")])
    plan = llm.structured(user(), ResearchPlan, think=True)
    assert plan.sub_questions[0].id == "Q1"
    body = sent[0]
    assert body["think"] is True and body["stream"] is False
    assert body["format"] == ResearchPlan.model_json_schema()
    assert body["options"]["num_ctx"] == 8192
    assert "schéma JSON" in body["messages"][-1]["content"]       # le schéma est montré au modèle
    assert body["messages"][0]["content"] == "sys"                  # le message système n'est pas modifié
    assert llm.stats.calls == 1 and llm.stats.prompt_tokens == 100


def test_structured_retries_with_error_feedback():
    llm, sent = make_llm([('{"sub_questions": "pas une liste"}', "stop"), (PLAN_JSON, "stop")])
    plan = llm.structured(user(), ResearchPlan)
    assert plan.initial_queries == ["a", "b"]
    assert len(sent) == 2 and llm.stats.retries == 1
    assert sent[1]["messages"][-1]["content"].startswith("Ce JSON est invalide")


def test_structured_truncated_while_thinking_retries_without_thinking():
    llm, sent = make_llm([('{"sub_q', "length"), (PLAN_JSON, "stop")])
    llm.structured(user(), ResearchPlan, think=True)
    assert sent[0]["think"] is True and sent[1]["think"] is False


def test_structured_gives_up_after_retries():
    llm, _ = make_llm([("pas du json", "stop")])
    with pytest.raises(LLMError, match="ResearchPlan"):
        llm.structured(user(), ResearchPlan, retries=1)
    assert llm.stats.failures == 1


def test_context_overflow_is_refused_before_calling():
    llm, sent = make_llm([(PLAN_JSON, "stop")], num_ctx=1000, max_output_tokens=500, chars_per_token=3.0)
    with pytest.raises(ContextOverflowError):
        llm.structured(user("x" * 3000), ResearchPlan)
    assert sent == []


def test_think_tags_are_stripped_from_chat():
    llm, _ = make_llm([("<think>réflexion</think>\nRéponse finale", "stop")])
    assert llm.chat(user()) == "Réponse finale"


def test_chat_truncated_raises():
    llm, _ = make_llm([("début de rép", "length")])
    with pytest.raises(LLMError, match="tronquée"):
        llm.chat(user())


def test_http_error_is_wrapped():
    def handler(request):
        return httpx.Response(500)
    llm = OllamaLLM("http://ollama.test", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError):
        llm.chat(user())


@pytest.mark.parametrize("schema", [ResearchPlan, UrlSelection, Extraction, SubQuestionAssessment, ClaimVerdict])
def test_schemas_are_flat_enough(schema):
    """Garde-fou : au plus un niveau d'objets imbriqués (lisible par un modèle 8B)."""
    s = schema.model_json_schema()
    for sub in s.get("$defs", {}).values():
        for prop in sub.get("properties", {}).values():
            assert "$ref" not in prop and "$ref" not in prop.get("items", {})
