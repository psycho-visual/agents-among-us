# Agents Among Us

A small, casual LLM eval: a social-deduction "imposter" game played by language models.

Four AI players sit at a table. Three of them are copies of the same model (the **trio**); one is a
different model (the **imposter**). They introduce themselves, take turns asking each other questions,
and then vote on who they think the imposter is. You watch it all happen live in your browser, including
each model's reasoning (when the API returns it) and exactly how much every call cost.

Models are reached through [OpenRouter](https://openrouter.ai), so one API key works for Claude, GPT and others.

---

## How to run it (step by step)

You need two things: **Python** and an **OpenRouter API key**. No other installs. The app uses only
Python's standard library and your normal web browser.

### 1. Install Python 3.11 or newer

Check if you already have it. Open a terminal (macOS: the *Terminal* app; Windows: *PowerShell*) and type:

```bash
python3 --version
```

On Windows, type `python --version` instead. If it says `Python 3.11` or higher (3.12, 3.13, …), you're set.
If not, download Python from <https://www.python.org/downloads/> and install it. On Windows, tick
**"Add python.exe to PATH"** on the first installer screen.

> On Windows, use `python` wherever this guide says `python3`.

### 2. Get the code

Either download the repository as a ZIP from GitHub (green **Code** button → **Download ZIP**) and unzip it,
or, if you use git:

```bash
git clone https://github.com/psycho-visual/agents-among-us.git
```

Then move into the folder in your terminal:

```bash
cd agents-among-us
```

### 3. Try it for free first (mock mode)

Mock mode uses fake, built-in players instead of real models. No API key, no cost. It's a good way to
see how the app works.

```bash
python3 run.py --mock
```

Your browser opens at <http://127.0.0.1:8765>. Click **Run whole game** and watch. Press `Ctrl+C` in the
terminal to stop the app when you're done.

### 4. Add your OpenRouter API key

1. Create an account at <https://openrouter.ai>, add a few dollars of credit, and create a key at
   <https://openrouter.ai/settings/keys>. It starts with `sk-or-`.
2. In the `agents-among-us` folder, create a plain-text file named exactly `.env` (note the leading dot)
   containing one line:

   ```
   OPENROUTER_API_KEY=sk-or-your-key-here
   ```

   The `.env` file is ignored by git, so your key won't be uploaded by accident.
   (Alternatively, set an environment variable named `OPENROUTER_API_KEY`.)

> Tip for Windows/macOS: text editors sometimes save the file as `.env.txt`. Make sure the name is just `.env`.

### 5. Run a real game

```bash
python3 run.py
```

In the browser:

| Button | What it does |
| --- | --- |
| **Next round ▶** | Runs only the next step (introductions, one round of questions, or a vote), then stops. |
| **Run whole game ⏩** | Plays automatically until the game ends. |
| **Pause ⏸** | Stops after the current turn finishes (a call already sent to a model can't be recalled). |
| **New game** | Starts a fresh game. It re-reads `config.toml`, so you can change settings without restarting. |

Expand **Reasoning** under any message to see what the model was thinking, if the API returned it.

### 6. Look at the results

Every game is saved in the `logs/` folder:

- `logs/games/<game-id>.md` is a readable transcript (open it in any text editor).
- `logs/games/<game-id>.json` is the full record: every prompt, answer, reasoning, vote and API call with its tokens and cost.
- `logs/summary.jsonl` holds one line per finished game, used for aggregate stats.

To see stats across all your games (win-loss per model, how often the imposter was caught, rounds survived, cost):

```bash
python3 analyze.py
```

Add `--csv results.csv` to export a spreadsheet. Mock games are kept separately in `logs/mock/` and are
left out unless you pass `--include-mock`.

---

## What reasoning and cost data you'll get

I checked OpenRouter's documentation, but no real API call has been made yet, so treat this as expected
behaviour until your first game confirms it:

- **Claude models** return a *summary* of their thinking, not the raw thinking.
- **OpenAI GPT models** never return their raw reasoning. At most you get a short summary, and sometimes nothing.
- Every API call is labelled in the log (`reasoning_kind`: `text`, `summary`, `encrypted_only` or `none`),
  and the app shows "No reasoning returned by API" when there was none.
- **Cost:** OpenRouter reports the exact USD cost of every call, and the app adds them up live.
  At the default models' listed prices ($0.10 per million input tokens, $0.50 per million output),
  the token volume of a mock game would cost about 1–2 cents. Real games may cost more or less depending
  on how much the models think. Measuring that is what the first run is for.
- Reasoning tokens count toward each call's `max_output_tokens`. If a model runs out of room before
  answering, the app automatically retries once with double the limit and notes this in the log.

## Changing the setup

The easiest way is the **Settings ⚙** button in the app. Every option has a plain-English explanation,
the model picker searches OpenRouter's model list with prices, and **Swap trio ↔ imposter** swaps the roles.
Saving writes to `config.toml` and keeps all its comments. The previous version is saved as
`config.toml.bak`, and changes apply when you click **New game**.

Everything is stored in **`config.toml`**, a plain-text file with comments, so you can also edit it in any text editor.

- **Models:** change `model` under `[trio]` and `[imposter]`. Use OpenRouter model IDs, which you can
  find at <https://openrouter.ai/models>. `display_name` is the name the trio is told they are.
- **Swap roles:** swap the contents of the `[trio]` and `[imposter]` sections.
- **Reasoning:** `reasoning_effort` is one of `none`, `low`, `medium`, `high`, `xhigh`, `max`.
- **Name rule:** `name_rule = "lie_allowed"` (players may claim to be any model) or
  `name_rule = "no_names"` (players may not name any model; slips are flagged, and blacked out if
  `redact_violations = true`).
- **Rounds before a vote:** `rounds_before_vote`.
- **No decision:** `on_no_decision = "end"` ends the game when nobody gets 3 votes; `"continue"` keeps
  playing and voting until `max_rounds`.
- **Player-called votes:** `player_vote_calls`, `vote_call_threshold`, `reveal_vote_calls` (see the rules below).
- **Fairness settings:**
  - `normalize_typography` (on by default) turns curly quotes, dashes and markdown into plain text
    before other players see it, so the trio can't catch the imposter by its apostrophes.
  - `imposter_knows_own_model` decides whether the imposter is told its own model name.
  - `no_imposter_chance` (0 to 1) sometimes plays a game with no imposter at all. Players are told the
    chance, may vote `NOBODY`, and the trio loses if they vote out one of their own copies.
  - The `[paraphrase]` section can rewrite every public message through a neutral third model
    before others see it (off by default).
- **Prompts:** click **Prompts ✎** in the app to edit any prompt the models see. You get a live check,
  a preview, and *Reset to default*. Edits are saved as text files in `prompts/` and apply when you
  click **New game**. Each game records a `prompt_version`, and `analyze.py` reports results per prompt
  version, so games played with different prompts never get mixed up (`--prompt-version` filters to one).

## The rules, exactly

1. **Introductions.** The trio write their introductions without seeing each other's. The imposter
   writes last, after reading the trio's. All four are then shown to everyone in seat order, so the
   order doesn't reveal who wrote last.
2. **Rounds.** On each turn one player asks a question and every player (asker included) answers it.
   By default answers are written blind and revealed together (`answer_mode = "simultaneous"`). A round
   ends when every player has asked once.
3. **Votes** happen automatically after every `rounds_before_vote` rounds. Everyone votes in secret, the
   imposter included, and no one can vote for themselves. A player with 3 or more votes is out.
   - Imposter out → **trio wins**.
   - A trio member out → **imposter wins**.
   - Nobody reaches 3 → **no decision** (with the default settings).
4. **Players can call a vote** (`player_vote_calls = true`). Every answer ends with a hidden line,
   `CALL VOTE: YES` or `CALL VOTE: NO`, which is removed before other players see the answer. If at least
   `vote_call_threshold` players (default 3) say YES after the same question, a vote is held right away.
   A called vote that removes nobody doesn't end the game; play just continues. Requests are secret unless
   `reveal_vote_calls = true`. Set `player_vote_calls = false` to go back to scheduled votes only.
5. **What players know.** Everyone knows whether they are the imposter. Trio members are told their own
   model name and that two other players are copies of themselves (not which ones). The imposter is not
   told which model the trio is.

**Baseline:** if everyone voted at random, the imposter would be caught 1 time in 4 among games that end
in an elimination. `analyze.py` compares your results against that.

## Project layout

```
run.py                  start here: launches the app and opens your browser
config.toml             all settings (models, reasoning, rules)
analyze.py              aggregate stats across saved games
imposter_game/
  default_prompts/      every prompt the models see (edit them in the app; your edits go to prompts/)
  prompts.py            loads, checks and fills in the prompt templates
  game.py               game rules and flow
  openrouter.py         talks to OpenRouter (plus the fake "mock" players)
  logger.py, stats.py   log files and statistics
  config.py, server.py  config loading and the local web server
static/                 the browser page (HTML/CSS/JavaScript)
tests/                  automated tests: python3 -m unittest discover -s tests
```
