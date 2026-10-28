# VLe

One question in. REA reads the code first. A skill router picks how to work with you. Three smaller agents argue. The Judge picks one winner. Humanizer rewrites that answer so it reads like a person.

VLe runs as a local Cursor agent through the Python package `cursor-sdk`. Each step is a Cursor agent run on your machine. Sign-in is a browser login. VLe does not use `CURSOR_API_KEY`.

Rules are not in the code. They live in [RULES.md](RULES.md), numbered, and are loaded into every sub-agent at run time. Skills are loaded the same way from `skills/`.

## How a question flows

1. **REA** — If you name a path, or ask how the code works, a local agent with only `read`, `grep`, `glob`, and `ls` opens that tree and returns an evidence brief. No path means the brief is `no target`. This step explains the code. It does not write exploits or bypass steps.
2. **Skill router** — Follows `using-agent-skills` and an index of names plus descriptions. It selects at most two skills. If it selects `interview-me`, VLe asks that one question and stops.
3. **Round 1** — Direct, Skeptic, and Checker each answer alone, from a different stance. They are text-only agents. They cannot edit the repo.
4. **Round 2** — Each debater, on the same agent, attacks the weakest rival claim by name, concedes any point it lost, and revises.
5. **Judge** — A fresh one-shot agent scores the revised answers: no invented facts, exact question, concrete, survived the attacks, shortest. One winner. No blending.
6. **Humanizer** — Rewrites the winning prose. Claims, names, and numbers stay. JSON, code, and evidence lines stay.

You receive `[Winner: <name>]` and the humanized answer. Run with `--show-debate` to print the brief, the skills chosen, both rounds, the verdict, and the rewrite.

A plain question is 9 Cursor agent runs: the router, three debaters times two rounds, the Judge, and Humanizer. A code question adds the REA run. Those runs are billed on your Cursor account.

## Files

| File | What it is |
| --- | --- |
| `agent.py` | The pipeline and the Cursor calls. |
| `vle.py` | Launcher: `py vle.py "your question"`. |
| `RULES.md` | Precision, debate, and pipeline rules. Edit this to change how VLe answers. |
| `skills/rea/` | `reverse-engineer-anything` from [morluto/rea](https://github.com/morluto/rea), plus its MIT license. |
| `skills/engineering/` | 25 skills and `references/` from [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills), plus its MIT license. |
| `skills/ux/` | Five skills from [uxuiprinciples/agent-skills](https://github.com/uxuiprinciples/agent-skills). |
| `skills/humanizer/` | [blader/humanizer](https://github.com/blader/humanizer), plus its MIT license. |
| `knowledge.txt` | Facts VLe may use. Injected only when the file has text, capped at 2000 characters. |

`UXUI_API_KEY` is optional. Without it, the UX skills still run and say they are not API-enriched.

## Run it

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py vle.py "your question"
py vle.py --show-debate "your question"
```

The first live question opens a browser so you can sign in to Cursor. Node.js 22 or newer is required for that sign-in step. The Cursor SDK writes the login to `~/.cursor/sdk/auth.json` on this computer. Later questions reuse that file. Nothing is placed in the environment, and the key is not printed.

The model defaults to `composer-2.5` because a local Cursor agent requires one. Override it with `CURSOR_MODEL`. Runs stay on this machine, against the folder you point at.

The fake runtime walks the whole pipeline offline, with no sign-in and no network:

```powershell
py agent.py --selftest
```

Mac/Linux: same steps with `python3`.

## Customize

- Rules: edit `RULES.md`.
- Sub-agents: edit `DEBATERS` in `agent.py`.
- Facts: edit `knowledge.txt`.
- Skills: replace a folder under `skills/` when the upstream pack changes. The code reads whatever `SKILL.md` files are there.
