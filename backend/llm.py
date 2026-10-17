"""LiteLLM wrapper: one answer call over the retrieval package, then a citation check."""
import json
import os
import re

import litellm

MODELS = {
    "groq": os.getenv("GROQ_MODEL", "groq/llama-3.3-70b-versatile"),
    "claude": os.getenv("CLAUDE_MODEL", "anthropic/claude-opus-5-5"),
    "openai": os.getenv("OPENAI_MODEL", "openai/gpt-4.1-mini"),
}
KEY_ENV = {"groq": "GROQ_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
CITE_RE = re.compile(r"\[node:([A-Za-z]+:[0-9a-f]{8})\]")

ANSWER_SYSTEM = """You are RepoMemory, a debugging assistant with project memory.
You are given a CONTEXT PACKAGE retrieved from a knowledge graph of the repository (components, decisions with
reasons, bugs, fixes, failed attempts, dependency changes, coupled files). You never see the code itself.

Rules:
- Answer ONLY from the package. If the package does not contain something, say so.
- Cite every factual claim with the node id exactly as written, e.g. [node:decision:1a2b3c4d]. Never invent ids.
- Say what is uncertain. If a fix has verified=false, say it is unverified.
- If a failed attempt is relevant, warn against repeating it.
- End with "Next step:" — one concrete debugging step.
Keep it under 250 words. Use short paragraphs or bullets."""

DRAFT_SYSTEM = """You write commit decision records. Given a git diff and context about the touched components,
return JSON {"summary": "<= 15 words, what this change decides/fixes", "reason": "1-3 sentences: why, referencing
the prior decisions/bugs from context when relevant"}. Be concrete, no marketing language."""


def available_models() -> list[dict]:
    return [{"id": k, "model": v, "available": bool(os.getenv(KEY_ENV[k]))} for k, v in MODELS.items()]


def _complete(model_key: str, system: str, user: str, json_mode=False) -> str:
    model = MODELS.get(model_key, model_key)
    kwargs = {"max_tokens": 4000 if model.startswith("anthropic/") else 1200}
    if not model.startswith("anthropic/"):
        kwargs["temperature"] = 0.2  # current Claude models reject sampling params
    if json_mode and not model.startswith("anthropic/"):
        kwargs["response_format"] = {"type": "json_object"}
    resp = litellm.completion(model=model, messages=[{"role": "system", "content": system},
                                                     {"role": "user", "content": user}], **kwargs)
    return resp.choices[0].message.content or ""


def check_citations(answer: str, valid_uids: set[str]) -> tuple[str, list[str], list[str]]:
    """Returns (answer with invalid citations flagged, valid cited uids, invalid uids)."""
    cited, invalid = [], []
    for u in CITE_RE.findall(answer):
        (cited if u in valid_uids else invalid).append(u)
    for u in set(invalid):
        answer = answer.replace(f"[node:{u}]", f"[invalid citation: {u}]")
    return answer, list(dict.fromkeys(cited)), sorted(set(invalid))


def answer(package: dict, model_key: str) -> dict:
    text = _complete(model_key, ANSWER_SYSTEM, "CONTEXT PACKAGE:\n\n" + package["package_text"])
    valid = set(CITE_RE.findall(package["package_text"]))
    text, cited, invalid = check_citations(text, valid)
    return {"answer": text, "citations": cited, "invalid_citations": invalid, "model": MODELS.get(model_key, model_key)}


def draft_decision(diff: str, package_text: str, model_key: str) -> dict:
    user = f"CONTEXT:\n{package_text}\n\nDIFF:\n{diff[:12000]}"
    raw = _complete(model_key, DRAFT_SYSTEM, user, json_mode=True)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m[0] if m else raw)
        return {"summary": str(data.get("summary", "")), "reason": str(data.get("reason", ""))}
    except Exception:
        return {"summary": raw.strip().splitlines()[0][:120] if raw.strip() else "", "reason": raw.strip()}
