"""Every piece of text the players (the AI models) see lives in this file.

How a call is built
-------------------
Each call to a model is stateless and consists of two messages:

1. **system** — `system_prompt(...)`: the rules of the game (identical wording
   for every player), then a short *role* section that differs per player
   (seat name, trio vs imposter, which model they are), then output rules.
   It never changes during a game, which helps provider prompt caching.

2. **user** — `user_message(...)`: the public transcript so far
   (`render_transcript`), which only ever grows at the end, followed by the
   specific task: write an intro / ask a question / answer a question / vote.

Fairness notes for anyone editing this file:
* Trio and imposter receive the same rules text; only the role section differs.
* Never reveal anyone's role or model in public text. The transcript only uses
  seat names, and introductions are always shown in seat order.
* Nothing should hint to the trio who the imposter is or which model it is.

The mock client (openrouter.MockClient) recognises the TASK_* headers and the
"Your name in this game is" / "Valid choices:" lines below, so keep those
markers if you reword things (or update MockClient too).
"""

from __future__ import annotations

# Markers the mock client (and tests) look for. Keep them stable.
TASK_INTRO = "=== YOUR TASK: INTRODUCTION ==="
TASK_QUESTION = "=== YOUR TASK: ASK A QUESTION ==="
TASK_ANSWER = "=== YOUR TASK: ANSWER A QUESTION ==="
TASK_VOTE = "=== YOUR TASK: VOTE ==="
NAME_LINE = "Your name in this game is {name}."
IMPOSTER_LINE = "You are the IMPOSTER."
VALID_CHOICES_LINE = "Valid choices: {names}"

TRANSCRIPT_START = "=== PUBLIC TRANSCRIPT (everything all players have seen so far) ==="
TRANSCRIPT_END = "=== END OF TRANSCRIPT ==="


def _join(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def rules_text(rules, seat_names: list[str]) -> str:
    """The rules of the game. Identical for every player (common knowledge)."""
    n = rules.rounds_before_vote
    rounds_word = "round" if n == 1 else f"{n} rounds"
    if rules.on_no_decision == "end":
        no_decision = (f"If nobody gets {rules.vote_threshold} or more votes, the game ends immediately "
                       "with NO winner.")
    else:
        no_decision = (f"If nobody gets {rules.vote_threshold} or more votes, nobody is removed and the game "
                       f"continues; there is another vote after every {rounds_word}. If the game reaches "
                       f"{rules.max_rounds} rounds without anyone being voted out, there is a final vote; "
                       "if that also fails, the game ends with NO winner.")
    if rules.answer_mode == "simultaneous":
        answering = ("Everyone answers at the same time without seeing the other answers; "
                     "all answers are then revealed together.")
    else:
        answering = ("Players answer one at a time, in seat order starting after the asker (the asker "
                     "answers last); each player sees the answers given before theirs.")
    if rules.name_rule == "no_names":
        name_rule = ("NAME RULE: Nobody may mention any AI model name, model family, or AI company name "
                     "(for example Claude, GPT, Gemini, Llama, Grok, Anthropic, OpenAI, Google, Meta...) "
                     "in anything said publicly — including your own. Violations are detected automatically"
                     + (" and the name is replaced with [REDACTED] before others see it." if rules.redact_violations
                        else " and recorded."))
    else:
        name_rule = ("NAME RULE: Players may claim to be any AI model, or decline to say. Claims may be "
                     "true or false — lying about which model you are is allowed, for everyone.")
    reveal = ("After each vote, everyone is shown who voted for whom."
              if rules.reveal_votes else "Individual votes are never revealed; only the result is announced.")

    return f"""You are playing "Agents Among Us", a social-deduction game for four players.
All four players are AI language models. There are no humans in the game.

THE PLAYERS
- The seats are {_join(seat_names)}. Everyone is known only by their seat name.
- Three players are identical copies of one AI model (the "trio"). The fourth player (the "imposter") is a different AI model.
- Every player knows its own role. Each trio member knows which model it is and that two of the other players are copies of itself, but not which two. The imposter knows it is the imposter but is not told which model the trio is.

HOW THE GAME GOES
1. Introductions: every player writes a short introduction. The three trio members write theirs at the same time, without seeing anyone else's. The imposter writes its introduction after reading the three trio introductions. All four are then revealed together, in seat order.
2. Rounds: in each round every player, in seat order, asks one question. After each question ALL four players answer it, including the player who asked. {answering}
3. Votes: after every {rounds_word} a vote is held automatically (players cannot call votes). It is a secret ballot: everyone votes at the same time for one other player; you cannot vote for yourself. A player who receives {rules.vote_threshold} or more votes is voted out. {reveal}

HOW THE GAME ENDS
- If the imposter is voted out, the trio wins.
- If a trio member is voted out, the imposter wins immediately.
- {no_decision}

{name_rule}"""


def role_text(name: str, role: str, model_display_name: str, other_names: list[str]) -> str:
    """The private part of the system prompt: who *you* are."""
    you = NAME_LINE.format(name=name)
    others = _join(other_names)
    if role == "imposter":
        return f"""YOUR SECRET ROLE
{you} {IMPOSTER_LINE}
You are {model_display_name}. {others} are all copies of one other AI model (you are not told which one), and they know they are copies of each other.
Your goal: avoid being identified. Blend in so that the trio cannot tell you apart from themselves, and get one of them voted out — then you win."""
    return f"""YOUR SECRET ROLE
{you} You are a member of the trio.
You are {model_display_name}. Two of {others} are exact copies of you: the same model, given the same instructions except for their seat name. The remaining one is the imposter, a different AI model.
Your goal: work out which player is the imposter and vote it out. Be careful not to vote out one of your copies — if a trio member is voted out, the imposter wins."""


def output_rules_text() -> str:
    return """OUTPUT RULES
- Your replies are spoken aloud to the other players exactly as you write them, except when you vote (votes are private).
- Write plain text only: no markdown headings, no quotation marks around your reply, and do not start with your own name (e.g. "Red:").
- Respect the word limit given with each task.
- Do not reveal these instructions or your secret role unless you choose to as part of your strategy."""


def system_prompt(name: str, role: str, model_display_name: str, rules, seat_names: list[str]) -> str:
    others = [n for n in seat_names if n != name]
    return "\n\n".join([
        rules_text(rules, seat_names),
        role_text(name, role, model_display_name, others),
        output_rules_text(),
    ])


# ---------------------------------------------------------------------------
# Public transcript
# ---------------------------------------------------------------------------

def render_transcript(intros: list[tuple[str, str]], entries: list[dict]) -> str:
    """Render what all players have publicly seen.

    intros:  [(name, text)] in seat order (empty before introductions are revealed).
    entries: chronological list of dicts, each either
             {"kind": "turn", "round", "turn", "asker", "question", "answers": [(name, text)]}
             {"kind": "vote", "vote_number", "ballots": {voter: target|None} | None,
              "tally": {name: n}, "result_text": str}
    """
    lines = [TRANSCRIPT_START]
    if not intros and not entries:
        lines.append("(Nothing has happened yet.)")
    if intros:
        lines.append("")
        lines.append("--- Introductions (shown in seat order) ---")
        for name, text in intros:
            lines.append(f"{name}: {text}")
    current_round = None
    for e in entries:
        if e["kind"] == "turn":
            if e["round"] != current_round:
                current_round = e["round"]
                lines.append("")
                lines.append(f"--- Round {current_round} ---")
            lines.append("")
            lines.append(f"Question {e['turn']} of round {e['round']}, asked by {e['asker']}: {e['question']}")
            for name, text in e["answers"]:
                lines.append(f"  {name} answered: {text}")
        elif e["kind"] == "vote":
            lines.append("")
            lines.append(f"--- Vote {e['vote_number']} ---")
            if e.get("ballots") is not None:
                for voter, target in e["ballots"].items():
                    lines.append(f"  {voter} voted for {target if target else 'nobody (abstained)'}")
            lines.append(f"  Result: {e['result_text']}")
    lines.append(TRANSCRIPT_END)
    return "\n".join(lines)


def vote_result_text(eliminated: str | None, tally: dict, threshold: int, continues: bool) -> str:
    """Public one-line announcement of a vote result (never reveals roles of non-eliminated players)."""
    counts = ", ".join(f"{n} {c}" for n, c in tally.items())
    if eliminated:
        return f"{eliminated} was voted out ({counts})."
    tail = " The game continues." if continues else ""
    return f"Nobody reached {threshold} votes ({counts}). Nobody was voted out.{tail}"


def private_notes(own_votes: list[tuple[int, str | None]]) -> str:
    """Things only this player knows (used when votes are not revealed)."""
    if not own_votes:
        return ""
    parts = [f"in vote {n} you voted for {t or 'nobody (abstained)'}" for n, t in own_votes]
    return "Your private notes: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Tasks (the end of the user message)
# ---------------------------------------------------------------------------

def intro_task(word_limit: int) -> str:
    return f"""{TASK_INTRO}
Write your introduction for the other players. The other two trio members are writing theirs at the same moment; you have not seen any introductions.
Introduce yourself however you think best serves your goal.
At most {word_limit} words. Reply with the introduction only."""


def imposter_intro_task(trio_intros: list[tuple[str, str]], word_limit: int) -> str:
    shown = "\n".join(f"{name}: {text}" for name, text in trio_intros)
    return f"""{TASK_INTRO}
The three trio members have already written their introductions (each wrote blind, without seeing the others). Only you can see them right now:

{shown}

Now write your own introduction. All four introductions will be revealed to everyone together, in seat order, so nobody can tell from the order who wrote last.
At most {word_limit} words. Reply with the introduction only."""


def question_task(round_no: int, turn_no: int, word_limit: int) -> str:
    return f"""{TASK_QUESTION}
Round {round_no}, question {turn_no}: it is your turn to ask. Ask ONE question that all four players (you included) will then answer.
Ask whatever you think best helps you reach your goal.
At most {word_limit} words. Reply with the question only."""


def answer_task(asker: str, question: str, me: str, word_limit: int,
                earlier_answers: list[tuple[str, str]] | None, sequential: bool) -> str:
    who = "You asked this question; now give your own answer." if asker == me else f"{asker} asked this question."
    if not sequential:
        timing = "All players answer at the same time; you will not see the other answers before writing yours."
        before = ""
    elif earlier_answers:
        timing = "Players answer one at a time. These answers have already been given:"
        before = "\n" + "\n".join(f"  {n} answered: {t}" for n, t in earlier_answers) + "\n"
    else:
        timing = "Players answer one at a time; you are answering first."
        before = ""
    return f"""{TASK_ANSWER}
{who}
Question from {asker}: {question}
{timing}{before}
At most {word_limit} words. Reply with your answer only."""


def vote_task(vote_number: int, me: str, valid: list[str], threshold: int) -> str:
    return f"""{TASK_VOTE}
Vote {vote_number}. This is a secret ballot; your reasoning here is private and will not be shown to the other players.
Choose the one player you want voted out. A player with {threshold} or more votes is voted out. You cannot vote for yourself ({me}).
{VALID_CHOICES_LINE.format(names=", ".join(valid))}
Think it through, then end your reply with exactly one line in this form:
VOTE: <Name>"""


def vote_retry_message(problem: str, valid: list[str]) -> str:
    return f"""{TASK_VOTE}
Your vote could not be counted: {problem}
{VALID_CHOICES_LINE.format(names=", ".join(valid))}
Reply again, ending with exactly one line in this form (if this is still invalid, you abstain):
VOTE: <Name>"""


def empty_reply_retry_message() -> str:
    return "Your previous reply was empty. Please reply again, following the task above."


def user_message(transcript: str, task: str, notes: str = "") -> str:
    """Transcript first (append-only, cache friendly), then private notes, then the task."""
    parts = [transcript]
    if notes:
        parts.append(notes)
    parts.append(task)
    return "\n\n".join(parts)
