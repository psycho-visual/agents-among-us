"""Writing game logs to disk.

For each game (mock games go under <log_dir>/mock/ instead of <log_dir>/):

    games/<game_id>.json   full machine-readable record, rewritten after every step
    games/<game_id>.md     human-readable transcript, rewritten after every step
    summary.jsonl          one line appended per finished game (read by analyze.py)

Files are written atomically (temp file + rename) so a crash never leaves a
half-written JSON file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .config import REPO_ROOT


def resolve_log_dir(log_dir: str | Path | None) -> Path:
    """Relative log dirs are relative to the repo root, not the current directory."""
    p = Path(log_dir) if log_dir is not None else Path("logs")
    return p if p.is_absolute() else REPO_ROOT / p


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class GameLogger:
    def __init__(self, log_dir: str | Path | None, game_id: str, mock: bool):
        base = resolve_log_dir(log_dir)
        self.root = base / "mock" if mock else base
        self.games_dir = self.root / "games"
        self.json_path = self.games_dir / f"{game_id}.json"
        self.md_path = self.games_dir / f"{game_id}.md"
        self.summary_path = self.root / "summary.jsonl"
        self._lock = threading.Lock()

    def write_record(self, record: dict) -> None:
        with self._lock:
            _atomic_write(self.json_path, json.dumps(record, indent=1, ensure_ascii=False, default=str))

    def write_transcript(self, markdown: str) -> None:
        with self._lock:
            _atomic_write(self.md_path, markdown)

    def append_summary(self, row: dict) -> None:
        with self._lock:
            self.summary_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.summary_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Markdown transcript
# ---------------------------------------------------------------------------

def _quote(text: str | None) -> str:
    text = (text or "").strip() or "(empty)"
    return "\n".join("> " + line for line in text.splitlines())


def _money(x) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def render_markdown(record: dict) -> str:
    """Build the human-readable transcript from a game record (see Game._record)."""
    players = record.get("players", [])
    role_of = {p["name"]: p for p in players}
    out = [f"# Agents Among Us — game {record.get('game_id')}", ""]
    if record.get("mock"):
        out += ["*Mock game (fake models, no API calls).*", ""]
    if record.get("has_imposter") is False:
        out += ["**No imposter this game:** all four players are copies of the trio model.", ""]
    rules = record.get("config", {}).get("rules", {})
    out += [f"- Started: {record.get('started_at')}",
            f"- Rules: name rule `{rules.get('name_rule')}`, answers `{rules.get('answer_mode')}`, "
            f"vote every {rules.get('rounds_before_vote')} round(s), {rules.get('vote_threshold')} votes to remove, "
            f"no decision → `{rules.get('on_no_decision')}`",
            f"- Prompt version: `{record.get('prompt_version', 'legacy')}`"
            + (" (modified)" if record.get("prompts_modified") else ""),
            "", "| Seat | Name | Role | Model | Effort |", "|---|---|---|---|---|"]
    for p in players:
        out.append(f"| {p['seat']} | {p['name']} | {p['role']} | {p['display_name']} (`{p['model']}`) "
                   f"| {p.get('reasoning_effort') or 'default'} |")
    out.append("")

    for ev in record.get("events", []):
        t = ev.get("type")
        if t == "phase":
            out += [f"## {ev.get('label')}", ""]
        elif t == "message":
            who = f"**{ev['player']}** ({role_of.get(ev['player'], {}).get('role', '?')})"
            if ev.get("kind") == "question":
                out += [f"### {ev['player']} asks (round {ev.get('round')}, question {ev.get('turn')})", ""]
                out += [_quote(ev.get("text")), ""]
            else:
                flag = ""
                if ev.get("kind") == "answer" and "wants_vote" in ev:
                    flag = {True: " — *asked for a vote*", False: "", None: " — *(no vote-request line)*"}[
                        ev.get("wants_vote")]
                out += [who + flag + ":", "", _quote(ev.get("text")), ""]
            if ev.get("original_text") and ev.get("original_text") != ev.get("text"):
                what = ", ".join(ev.get("transforms") or []) or "cleanup"
                out += [f"<details><summary>original text (changed: {what})</summary>", "",
                        _quote(ev["original_text"]), "", "</details>", ""]
            if ev.get("reasoning"):
                out += ["<details><summary>reasoning</summary>", "", _quote(ev["reasoning"]), "", "</details>", ""]
        elif t == "rule_violation":
            out += [f"*Name-rule violation by {ev['player']}: {', '.join(ev.get('matches', []))}"
                    f"{' (redacted)' if ev.get('redacted') else ''}*", ""]
        elif t == "vote_call_check":
            req = ", ".join(ev.get("requesters") or []) or "none"
            out += [f"*Vote requests after this question: {req} ({ev.get('count')} of {ev.get('threshold')} needed)"
                    + (" → **a vote is called**" if ev.get("triggered") else "") + "*", ""]
        elif t == "vote_cast":
            target = ev.get("target") or ("NOBODY" if ev.get("abstain_reason") == "nobody" else "abstained")
            flag = "" if ev.get("valid") else " (invalid → abstain)"
            out += [f"**{ev['player']}** votes **{target}**{flag}", "", _quote(ev.get("reasoning") or ev.get("raw_text")), ""]
        elif t == "vote_result":
            tally = ", ".join(f"{k}: {v}" for k, v in ev.get("tally", {}).items())
            elim = ev.get("eliminated")
            out += [f"**Vote {ev.get('vote_number')} ({ev.get('trigger', 'scheduled')}) result:** {tally}. "
                    + (f"{elim} ({ev.get('eliminated_role')}) is voted out." if elim else "Nobody is voted out.")
                    + f" → `{ev.get('decision')}`", ""]
        elif t == "error":
            out += [f"**Error{' (fatal)' if ev.get('fatal') else ''}:** {ev.get('message')}", ""]
        elif t == "game_over":
            out += ["## Game over", "",
                    f"- Outcome: **{ev.get('outcome')}** (winner: {ev.get('winner') or 'none'})",
                    f"- Eliminated: {ev.get('eliminated') or 'nobody'}"
                    + (f" ({ev.get('eliminated_role')}), after {ev.get('rounds_survived')} round(s)"
                       + (f", in a {ev['eliminated_by']} vote" if ev.get("eliminated_by") else "")
                       if ev.get("eliminated") else ""),
                    f"- Rounds completed: {ev.get('rounds_completed')}"
                    + (f" ({ev.get('turns_completed')} question turns)" if ev.get("turns_completed") is not None
                       else "")]
            if ev.get("reason"):
                out.append(f"- Reason: {ev['reason']}")
            out.append("")

    # Cost table per player
    calls = record.get("calls", [])
    out += ["## Cost", "", "| Player | Model | Calls | Input tok | Output tok | Reasoning tok | Cost |",
            "|---|---|---|---|---|---|---|"]
    for p in players:
        mine = [c for c in calls if c.get("player") == p["name"]]
        cost = sum(c.get("cost_usd") or 0 for c in mine)
        out.append(f"| {p['name']} | {p['display_name']} | {len(mine)} | "
                   f"{sum(c.get('input_tokens') or 0 for c in mine)} | {sum(c.get('output_tokens') or 0 for c in mine)} | "
                   f"{sum(c.get('reasoning_tokens') or 0 for c in mine)} | {_money(cost)} |")
    totals = record.get("totals", {})
    out += [f"| **Total** | | {totals.get('num_api_calls', len(calls))} | {totals.get('total_input_tokens', 0)} | "
            f"{totals.get('total_output_tokens', 0)} | | {_money(totals.get('total_cost_usd'))} |", ""]
    return "\n".join(out)
