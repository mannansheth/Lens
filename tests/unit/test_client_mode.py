import json
from types import SimpleNamespace

from lens.llm.answering import validate_generation
from lens.llm.prompts import messages
from lens.llm.schemas import generation_schema


def test_client_mode_uses_grounded_answer_contract_and_client_prompt():
    evidence = {"e1": {"excerpt": "ProfilePage renders a button labelled Change password.",
                       "file": "src/ProfilePage.jsx", "entity_names": ["ProfilePage"]}}
    context = SimpleNamespace(items=[{"entity_names": ["ProfilePage"]}], evidence=evidence,
                              retrieved_count=1, render=lambda: json.dumps({"items": []}))
    schema = generation_schema("client", context)
    assert "answer" in schema["properties"]
    assert "intent" not in schema["properties"]
    sent = messages("How do I change my password?", context, "client", schema)
    assert "ordinary person" in sent[0]["content"]
    assert "Never invent a screen" in sent[0]["content"]

    raw = json.dumps({"evidence_status": "sufficient", "answer": [
        {"text": "Open your profile and choose Change password.", "basis": "static_fact", "citations": ["e1"]}
    ]})
    answer = validate_generation("How do I change my password?", context, raw, "client")
    assert answer.answer[0].text.startswith("Open your profile")


def test_client_mode_rejects_ungrounded_navigation_labels():
    evidence = {"e1": {"excerpt": "A password is checked during sign in.",
                       "file": "auth.py", "entity_names": ["sign_in"]}}
    context = SimpleNamespace(items=[{}], evidence=evidence, retrieved_count=1)
    raw = json.dumps({"evidence_status": "sufficient", "answer": [
        {"text": "Open `Settings > Security > Reset password`.", "basis": "inference", "citations": ["e1"]}
    ]})
    try:
        validate_generation("How do I change my password?", context, raw, "client")
    except ValueError as error:
        assert "literal" in str(error)
    else:
        raise AssertionError("unsupported UI path should fail citation validation")
