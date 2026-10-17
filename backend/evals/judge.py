"""The judge: checks each answer's meaning, which the word graders in run.py can't.

Two models, each doing what it was best at in calibration (2026-10-17, against the owner's own labels):
- Jev 1.13 (TypeSafe), a decision model, DECIDES. It answers yes/no questions with a probability, at about
  $0.0003 per answer. It agreed with the owner on 6/6 true answers and caught 3/3 invented ones
  (probability of "grounded": 0.90-0.98 for true answers, 0.02-0.05 for invented ones), consistently across runs.
- Claude Haiku 5.5 only EXPLAINS: when Jev says an answer isn't grounded, Haiku quotes the unsupported claims,
  so a red check says what was wrong. Alone it was too strict (it flagged 3 of the owner's 6 true answers).

Both are a different family than the twin (GPT): models tend to approve answers written in their own style.
Focus (padding, unasked-for offers) is scored but doesn't block yet: neither judge matched the owner well enough.
"""

import json
import urllib.request
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, SecretStr

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"  # Jev isn't a chat model: it has its own endpoint
JEV_MODEL = "typesafe/jev-1.13"  # pinned version, not ~typesafe/jev-latest: a gate needs a fixed instrument
EXPLAINER_MODEL = "anthropic/claude-haiku-5.5"
THRESHOLD = 0.5  # calibration showed a wide gap around it (true answers >= 0.90, invented ones <= 0.05)

QUESTIONS = {
    "grounded": {
        "type": "noul",
        "instructions": "Every statement in `answer` about Ezz (his experience, projects, results, numbers, companies, "
        "places, dates, skills, tools, stories, opinions) is supported by `briefing` or `conversation`. Faithful "
        "paraphrases and summaries count as supported. Statements about what the twin did in this conversation are "
        "supported when `tool_calls` shows it: passing a question on to Ezz (record_unknown_question), noting or "
        "saving the visitor's contact details so Ezz can follow up (record_user_details).",
    },
    "focused": {
        "type": "noul",
        "instructions": "`answer` sticks to what `question` asked: it does not list facts, tools or achievements the "
        "question did not ask about, does not pad a reply with unrelated background after saying it does not know "
        "something, and does not ask for the visitor's email or offer a follow-up unless the visitor asked to get in "
        "touch.",
    },
}

EXPLAIN_PROMPT = """You check answers given by an AI "digital twin" of Ezz (Ahmed Ezzeldeen), an ADAS system \
engineer, which speaks as Ezz in the first person. List every statement in the answer about Ezz (experience, \
projects, results, numbers, companies, places, dates, skills, tools, stories, opinions) that the briefing below \
does not support. Faithful paraphrases of the briefing are supported. Statements about what the twin did in \
this conversation are supported when the tool calls show it (record_unknown_question: the question was passed on \
to Ezz; record_user_details: the visitor's contact details were noted for Ezz). Quote each unsupported statement.
----------
TWIN'S BRIEFING:
"""


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unsupported_claims: list[str]


class JudgeResult(BaseModel):
    grounded_p: float  # Jev's probability that every claim about Ezz is supported
    focused_p: float  # Jev's probability that the answer sticks to the question
    grounded: bool
    focused: bool
    unsupported_claims: list[str]  # Haiku's quotes, only when grounded is False
    cost: float


class Judge:
    def __init__(self, api_key: SecretStr, base_url: str, briefing: str) -> None:
        self.api_key = api_key
        self.briefing = briefing
        self.explainer = OpenAI(base_url=base_url, api_key=api_key.get_secret_value(), timeout=60, max_retries=2)

    def __call__(
        self, question: str, history: list[dict[str, str]], answer: str, tool_calls: list[dict[str, Any]]
    ) -> JudgeResult:
        calls = [{"name": call["name"], "arguments": call["arguments"]} for call in tool_calls]
        state = {"briefing": self.briefing, "conversation": history, "question": question, "tool_calls": calls}
        decision = self._decide({**state, "answer": answer})
        grounded_p, focused_p = decision["answers"]["grounded"]["noul"], decision["answers"]["focused"]["noul"]
        cost = float(decision["usage"].get("cost") or 0.0)

        claims: list[str] = []
        if grounded_p < THRESHOLD:
            claims, explain_cost = self._explain(question, answer, calls)
            cost += explain_cost
        return JudgeResult(
            grounded_p=grounded_p,
            focused_p=focused_p,
            grounded=grounded_p >= THRESHOLD,
            focused=focused_p >= THRESHOLD,
            unsupported_claims=claims,
            cost=cost,
        )

    def _decide(self, state: dict[str, Any]) -> dict[str, Any]:
        """One call to Jev: the state to judge plus the yes/no questions; it returns a probability per question"""
        request = urllib.request.Request(
            DECISIONS_URL,
            data=json.dumps({"model": JEV_MODEL, "state": state, "questions": QUESTIONS}).encode(),
            headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # fixed https URL above
            result: dict[str, Any] = json.loads(response.read())
        return result

    def _explain(self, question: str, answer: str, calls: list[dict[str, Any]]) -> tuple[list[str], float]:
        """Ask Haiku to quote the unsupported claims (structured output: it must return exactly this JSON shape)"""
        response = self.explainer.chat.completions.create(
            model=EXPLAINER_MODEL,
            messages=[
                {"role": "system", "content": EXPLAIN_PROMPT + self.briefing},
                {
                    "role": "user",
                    "content": f"QUESTION:\n{question}\n\nTOOLS THE TWIN CALLED:\n{json.dumps(calls) if calls else '(none)'}"
                    f"\n\nANSWER:\n{answer}",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "explanation", "strict": True, "schema": Explanation.model_json_schema()},
            },
            temperature=0,
            max_tokens=800,
        )
        explanation = Explanation.model_validate_json(response.choices[0].message.content or "")
        cost = float((response.usage.model_extra or {}).get("cost") or 0.0) if response.usage else 0.0
        return explanation.unsupported_claims, cost
