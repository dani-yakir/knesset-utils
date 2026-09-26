"""MCP ergonomics eval: run a fresh, MCP-only Claude agent over a question set and score it.

Each run is a headless `claude -p` process with every built-in tool disabled and only the
knesset MCP server attached (started over stdio from a given source tree against a frozen
snapshot DB), so baseline and candidate servers can be A/B tested on identical data.

    python eval/run_eval.py split --seed 7
    python eval/run_eval.py run --label baseline-1 --src <worktree>/src --split train
    python eval/run_eval.py grade eval/runs/baseline-1
    python eval/run_eval.py report eval/runs/*
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
QUESTIONS = EVAL / "questions.json"
SPLIT = EVAL / "split.json"
RUNS = EVAL / "runs"
DEFAULT_DB = ROOT / "data" / "scratch-2026-09-11.sqlite"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

AGENT_PROMPT = """You are answering questions about the Israeli parliament (the Knesset).
You have a set of `knesset` MCP tools that expose a database mirrored from the Knesset's
official open-data API. Answer every question below using ONLY what you can find through
those tools -- do not answer from memory. If the data does not let you answer, say so.

Work through all the questions. When you are done, end your reply with a single fenced
```json block mapping each question id to a short answer string, e.g.
{{"Q01": "...", "Q02": "..."}}

Questions:
{questions}
"""

JUDGE_PROMPT = """You are grading answers to questions about the Knesset against a gold answer.
For each question give a score: 1 = correct (all requested numbers/names match the gold,
allowing trivial formatting/rounding/transliteration differences), 0.5 = partially correct
(as defined in the notes, or one of two requested parts correct), 0 = wrong or missing.
Grade only against the gold and notes, never against your own knowledge.

Return ONLY a JSON object: {{"Q01": {{"score": 1, "why": "..."}}, ...}}

{items}
"""


def _load_questions() -> dict[str, dict]:
    return {q["id"]: q for q in json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]}


def cmd_split(args: argparse.Namespace) -> None:
    ids = sorted(_load_questions())
    rng = random.Random(args.seed)
    rng.shuffle(ids)
    half = len(ids) // 2
    split = {"seed": args.seed, "train": sorted(ids[:half]), "test": sorted(ids[half:])}
    SPLIT.write_text(json.dumps(split, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(split, indent=2))


def _claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        sys.exit("claude CLI not found on PATH")
    return exe


def cmd_run(args: argparse.Namespace) -> None:
    questions = _load_questions()
    ids = json.loads(SPLIT.read_text(encoding="utf-8"))[args.split]
    out = RUNS / args.label
    work = out / "cwd"  # empty dir: no CLAUDE.md, no project memory
    work.mkdir(parents=True, exist_ok=True)

    mcp_cfg = {
        "mcpServers": {
            "knesset": {
                "type": "stdio",
                "command": str(PYTHON),
                "args": ["-m", "knesset_utils.server.mcp_server"],
                "env": {
                    "PYTHONPATH": str(Path(args.src).resolve()),
                    "MCP_DB_PATH": str(Path(args.db).resolve()),
                    "PYTHONIOENCODING": "utf-8",
                },
            }
        }
    }
    cfg_path = out / "mcp.json"
    cfg_path.write_text(json.dumps(mcp_cfg, indent=2), encoding="utf-8")
    prompt = AGENT_PROMPT.format(questions="\n".join(f"{i}. {questions[i]['question']}" for i in ids))
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    (out / "meta.json").write_text(
        json.dumps({"label": args.label, "src": str(args.src), "split": args.split, "ids": ids,
                    "model": args.model, "db": str(args.db)}, indent=2),
        encoding="utf-8",
    )

    cmd = [
        _claude(), "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", args.model,
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", str(cfg_path),
        "--allowedTools", "mcp__knesset",
        "--setting-sources", "",
        "--no-session-persistence",
    ]
    t0 = time.monotonic()
    with open(out / "transcript.jsonl", "w", encoding="utf-8") as f:
        proc = subprocess.run(cmd, input=prompt, stdout=f, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", cwd=work, timeout=args.timeout)
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        (out / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
    metrics = analyze(out)
    metrics["wall_seconds"] = round(wall, 1)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in metrics.items() if k != "answers"}, indent=2, ensure_ascii=False))


def _events(out: Path):
    for line in (out / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            yield json.loads(line)


def analyze(out: Path) -> dict:
    tool_calls: list[dict] = []
    errors = 0
    error_samples: list[str] = []
    result: dict = {}
    final_text = ""
    for ev in _events(out):
        if ev.get("type") == "assistant":
            for block in ev["message"].get("content", []):
                if block.get("type") == "tool_use":
                    tool_calls.append({"name": block["name"].split("__")[-1], "input": block.get("input")})
                elif block.get("type") == "text":
                    final_text = block["text"]
        elif ev.get("type") == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    errors += 1
                    text = block.get("content")
                    if isinstance(text, list):
                        text = " ".join(c.get("text", "") for c in text if isinstance(c, dict))
                    error_samples.append(str(text)[:300])
        elif ev.get("type") == "result":
            result = ev
            final_text = ev.get("result") or final_text

    sigs = Counter(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in tool_calls)
    answers: dict = {}
    m = re.findall(r"```json\s*(\{.*?\})\s*```", final_text, re.S)
    if m:
        try:
            answers = json.loads(m[-1])
        except json.JSONDecodeError:
            pass
    return {
        "duration_s": round(result.get("duration_ms", 0) / 1000, 1),
        "num_turns": result.get("num_turns"),
        "cost_usd": result.get("total_cost_usd"),
        "tool_calls": len(tool_calls),
        "tool_calls_by_name": dict(Counter(c["name"] for c in tool_calls)),
        "tool_errors": errors,
        "duplicate_calls": sum(n - 1 for n in sigs.values() if n > 1),
        "is_error": result.get("is_error"),
        "error_samples": error_samples[:15],
        "answers": answers,
    }


def cmd_grade(args: argparse.Namespace) -> None:
    questions = _load_questions()
    for run in args.runs:
        out = Path(run)
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        ids = json.loads((out / "meta.json").read_text(encoding="utf-8"))["ids"]
        items = "\n\n".join(
            f"[{i}] Question: {questions[i]['question']}\nGold: {questions[i]['gold']}\n"
            f"Notes: {questions[i]['notes']}\nAnswer given: {metrics['answers'].get(i, '(missing)')}"
            for i in ids
        )
        proc = subprocess.run(
            [_claude(), "-p", "--output-format", "json", "--model", args.model, "--tools", "",
             "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"],
            input=JUDGE_PROMPT.format(items=items), capture_output=True, text=True, encoding="utf-8",
            cwd=out / "cwd",
        )
        text = json.loads(proc.stdout)["result"]
        grades = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        score = sum(float(g["score"]) for g in grades.values())
        (out / "grades.json").write_text(
            json.dumps({"score": score, "max": len(ids), "grades": grades}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"{out.name}: {score}/{len(ids)}")


def cmd_report(args: argparse.Namespace) -> None:
    cols = ["run", "score", "duration_s", "turns", "calls", "errors", "dupes", "cost", "by_tool"]
    print(" | ".join(cols))
    for run in args.runs:
        out = Path(run)
        m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        g = json.loads((out / "grades.json").read_text(encoding="utf-8")) if (out / "grades.json").exists() else {}
        print(" | ".join(str(x) for x in [
            out.name, f"{g.get('score', '?')}/{g.get('max', '?')}", m["duration_s"], m["num_turns"],
            m["tool_calls"], m["tool_errors"], m["duplicate_calls"], m["cost_usd"], m["tool_calls_by_name"],
        ]))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("split")
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_split)

    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--src", default=str(ROOT / "src"), help="source tree whose server is tested")
    r.add_argument("--split", choices=["train", "test"], required=True)
    r.add_argument("--model", default="sonnet")
    r.add_argument("--db", default=str(DEFAULT_DB))
    r.add_argument("--timeout", type=int, default=3600)
    r.set_defaults(fn=cmd_run)

    g = sub.add_parser("grade")
    g.add_argument("runs", nargs="+")
    g.add_argument("--model", default="sonnet")
    g.set_defaults(fn=cmd_grade)

    rp = sub.add_parser("report")
    rp.add_argument("runs", nargs="+")
    rp.set_defaults(fn=cmd_report)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
