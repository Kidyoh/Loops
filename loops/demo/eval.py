"""Eval set over the demo data: 20 questions with expected sources, plus extraction checks.

Run after `loops demo seed`:  loops eval [--mode tools|router] [--out report.md]
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loops.agent.agent import run_agent
from loops.db.models import Commitment, Message


@dataclass
class EvalQuestion:
    id: str
    question: str
    expected: list[str]  # demo message keys; empty means "should find nothing"


QUESTIONS: list[EvalQuestion] = [
    EvalQuestion("promises", "What did I promise people in the last two weeks?",
                 ["robi/2", "robi/4", "team/2", "meron/2", "hanna/2"]),
    EvalQuestion("waiting", "Who am I waiting on?", ["dawit/3", "dawit/2", "sara/2", "venue/1"]),
    EvalQuestion("postponed", "What has been postponed?", ["robi/5", "team/4", "venue/1"]),
    EvalQuestion("robi-pitch", "What did Robi say about the Habesha pitch?", ["robi/1", "robi/3"]),
    EvalQuestion("rehearsal-when", "When is the demo day rehearsal now?", ["team/4"]),
    EvalQuestion("rehearsal-where", "Where is the demo day rehearsal?", ["team/1"]),
    EvalQuestion("hanna-wants", "What does Hanna want from me?", ["hanna/1", "hanna/3"]),
    EvalQuestion("hanna-model", "Did I send Hanna the financial model yet?", ["hanna/2", "hanna/3"]),
    EvalQuestion("sara-confirm", "What did Sara ask me to confirm?", ["sara/2"]),
    EvalQuestion("vesting", "What vesting terms did I confirm?", ["sara/3"]),
    EvalQuestion("dawit-promise", "What did Dawit promise to deliver?", ["dawit/2", "team/3"]),
    EvalQuestion("intro", "Who asked me for an intro to Hanna, and did I agree?", ["robi/3", "robi/4"]),
    EvalQuestion("wedding", "What's happening on the 25th?", ["meron/1"]),
    EvalQuestion("budget", "What did I say about the Q4 budget?", ["budget/3", "budget/2"]),
    EvalQuestion("panel", "When is the AI Week panel?", ["liya/1"]),
    EvalQuestion("bio", "Did Liya get my bio and headshot?", ["liya/3"]),
    EvalQuestion("invoice", "How much was the October hosting invoice?", ["invoice/1"]),
    EvalQuestion("logo-format", "Which logo formats did I ask Dawit for?", ["dawit/1"]),
    EvalQuestion("neg-visa", "What did Tigist say about the visa application?", []),
    EvalQuestion("neg-berlin", "Did anyone mention the Berlin trip?", []),
]


@dataclass
class ExpectedLoop:
    kind: str
    key: str


# Open loops the demo data should produce.
REQUIRED_LOOPS = [
    ExpectedLoop("i_promised", "robi/2"),
    ExpectedLoop("i_promised", "robi/4"),
    ExpectedLoop("postponed", "robi/5"),
    ExpectedLoop("they_promised", "dawit/2"),
    ExpectedLoop("waiting_on", "dawit/3"),
    ExpectedLoop("i_promised", "team/2"),
    ExpectedLoop("postponed", "team/4"),
    ExpectedLoop("i_promised", "meron/2"),
    ExpectedLoop("they_promised", "sara/2"),
    ExpectedLoop("i_promised", "hanna/2"),
    ExpectedLoop("waiting_on", "venue/1"),
]
# Defensible either way: not required, not counted as false positives.
ACCEPTABLE_LOOPS = [
    ExpectedLoop("they_promised", "team/3"),
    ExpectedLoop("waiting_on", "sara/1"),
    ExpectedLoop("waiting_on", "dawit/1"),
    ExpectedLoop("waiting_on", "hanna/3"),
    ExpectedLoop("postponed", "venue/1"),
    ExpectedLoop("i_promised", "hanna/3"),
]
# Fulfilled inside their own thread: must not be left open.
MUST_NOT_BE_OPEN = ["budget/2", "liya/2"]


@dataclass
class QuestionResult:
    id: str
    question: str
    answer: str
    expected: list[str]
    cited: list[str]
    passed: bool
    recall: float | None
    precision: float | None
    error: str | None = None


def score_question(q: EvalQuestion, cited: list[str], answer: str, error: str | None = None) -> QuestionResult:
    if error:
        return QuestionResult(q.id, q.question, answer, q.expected, cited, False, None, None, error)
    if not q.expected:
        return QuestionResult(q.id, q.question, answer, q.expected, cited, not cited, None, None)
    hits = set(cited) & set(q.expected)
    recall = len(hits) / len(q.expected)
    precision = len(hits) / len(cited) if cited else 0.0
    return QuestionResult(q.id, q.question, answer, q.expected, cited, bool(hits), recall, precision)


@dataclass
class LoopResult:
    true_positives: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        found = len(self.true_positives) + len(self.false_positives)
        return len(self.true_positives) / found if found else 0.0

    @property
    def recall(self) -> float:
        total = len(self.true_positives) + len(self.missed)
        return len(self.true_positives) / total if total else 0.0


def score_loops(open_loops: list[tuple[str, str]]) -> LoopResult:
    """open_loops: (kind, demo key) for every open commitment."""
    required = {(e.kind, e.key) for e in REQUIRED_LOOPS}
    acceptable = {(e.kind, e.key) for e in ACCEPTABLE_LOOPS}
    found = set(open_loops)
    res = LoopResult()
    res.true_positives = sorted(f"{k} {key}" for k, key in required & found)
    res.missed = sorted(f"{k} {key}" for k, key in required - found)
    fps = {f"{k} {key}" for k, key in found - required - acceptable if key not in MUST_NOT_BE_OPEN}
    fps |= {f"{k} {key} (should be closed)" for k, key in found if key in MUST_NOT_BE_OPEN}
    res.false_positives = sorted(fps)
    return res


async def _keys_by_message_id(session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(select(Message.id, Message.external_id))).all()
    return {mid: ext for mid, ext in rows if "/" in ext}


async def run_eval(session: AsyncSession, llm, mode: str | None = None) -> dict:
    keys = await _keys_by_message_id(session)
    if not keys:
        raise RuntimeError("no demo data found; run `loops demo seed` first")

    results = []
    for q in QUESTIONS:
        answer, cited, error = "", [], None
        try:
            async for event in run_agent(session, llm, q.question, mode=mode):
                if event["type"] == "token":
                    answer += event["text"]
                elif event["type"] == "citations":
                    cited = [keys.get(c["message_id"], str(c["message_id"])) for c in event["items"]]
        except Exception as e:  # keep going; report the failure
            error = f"{type(e).__name__}: {e}"
            await session.rollback()
        results.append(score_question(q, cited, answer.strip(), error))

    open_rows = (
        await session.execute(select(Commitment.kind, Commitment.source_message_id).where(Commitment.status == "open"))
    ).all()
    loops = score_loops([(kind, keys[mid]) for kind, mid in open_rows if mid in keys])
    return {"questions": results, "loops": loops, "mode": mode or "default", "ran_at": datetime.now(UTC)}


def render_report(report: dict) -> str:
    qs: list[QuestionResult] = report["questions"]
    loops: LoopResult = report["loops"]
    answerable = [r for r in qs if r.expected]
    passed = sum(r.passed for r in qs)
    recalls = [r.recall for r in answerable if r.recall is not None]
    precisions = [r.precision for r in answerable if r.precision is not None]
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731

    lines = [
        f"# Eval report ({report['mode']} mode, {report['ran_at']:%Y-%m-%d %H:%M} UTC)",
        "",
        "## Q&A",
        "",
        f"- Passed: **{passed}/{len(qs)}** (answerable: cites at least one expected source; "
        "negative: cites nothing)",
        f"- Mean source recall: **{avg(recalls):.2f}**, mean citation precision: **{avg(precisions):.2f}**",
        "",
        "| | id | question | expected | cited |",
        "|---|---|---|---|---|",
    ]
    for r in qs:
        mark = "✅" if r.passed else "❌"
        cited = r.error or (", ".join(r.cited) or "none")
        lines.append(f"| {mark} | {r.id} | {r.question} | {', '.join(r.expected) or 'none'} | {cited} |")
    lines += [
        "",
        "## Open-loop extraction",
        "",
        f"- Precision **{loops.precision:.2f}**, recall **{loops.recall:.2f}**",
        f"- Found: {', '.join(loops.true_positives) or 'none'}",
        f"- Missed: {', '.join(loops.missed) or 'none'}",
        f"- False positives: {', '.join(loops.false_positives) or 'none'}",
        "",
        "## Answers",
        "",
    ]
    for r in qs:
        lines += [f"**{r.id}**: {r.question}", "", "> " + (r.answer or r.error or "(empty)").replace("\n", "\n> "), ""]
    return "\n".join(lines)
