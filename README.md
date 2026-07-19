# Gobo

A two-agent system for managing your own attention, over Telegram.

Once a day (and any time plans change — it's available 24/7), the **Planner**
("Good Cop", strong model) sits down with you, gathers your intentions and tasks,
and compiles them into a **hidden enforcement policy**: time-windows, trip-wires,
silence rules, and quietly-compressed deadlines you never see. A cheap, constant
**Manager** ("Bad Cop") executes it — handing you one task at a time from a queue
you can't read ahead in, checking in at task-scaled random intervals, verifying
completion conversationally, and assigning the next.

The invisibility is the core bet: you can't rules-lawyer a schedule you can't
see, so nudges land like an attentive observer reacting in the moment rather
than a cron job reading your own list back to you. And it's pointedly **not a
lie-detector** — it can't stop you drifting and doesn't try. Its leverage is the
interrupt itself: being asked *"what are you doing right now?"* forces the
conscious naming that a drift state survives by avoiding.

## Architecture

One asyncio process supervises three things: the Planner bot, the Manager bot,
and a ticker loop. All scheduling lives in a SQLite `timers` table — random
check-in times are drawn once and persisted, so restarts neither lose nor
duplicate pings. Inference runs through OpenRouter; each agent's model and
thinking level is set in `config.toml`.

The Manager model is kept honest **by construction**: each of its LLM calls
receives only the current task slice, tone, and disclosure rules — never the
queue, other tasks, or internal deadlines. It cannot leak what it was never
given.

```
src/gobo/
  app.py         supervisor: 2 bots + ticker
  scheduler.py   DB-backed timers, accelerable clock
  models.py      the policy schema + time-window math
  llm.py         OpenRouter (OpenAI SDK) + tool loop
  planner/       Good Cop: conversation, task tools, policy compilation
  manager/       Bad Cop: assignment, check-ins, escalation, trip-wires, DND
  cli.py         debug CLI — the only place the hidden policy is visible
```

## Setup

1. Create **two** bots via [@BotFather](https://t.me/BotFather) (e.g.
   `YourGoboPlannerBot`, `YourGoboManagerBot`). Give them distinct names/avatars.
2. Get your numeric Telegram user id (e.g. from [@userinfobot](https://t.me/userinfobot)).
3. Get an [OpenRouter API key](https://openrouter.ai/keys).
4. `cp .env.example .env` and fill it in. Adjust `config.toml` (timezone,
   models, tone, daily session time).

## Development (NixOS)

```sh
nix develop        # python + uv + pulumi + sqlite; uv pinned to nix python
uv sync
uv run python -m gobo          # run both bots
uv run pytest                  # offline test suite (fake clock, scripted LLM)
```

Useful dev knobs:

- `GOBO_TIME_SCALE=60` — virtual time runs 60× faster, so a full simulated day
  of check-ins takes minutes. `GOBO_TIME_START=2026-07-20T08:00` pins the start.
- `uv run python -m gobo.cli policy` — inspect the hidden policy (also:
  `tasks`, `timers`, `state`, `audit`, `messages`, `fire <timer-id>`).
  Deliberately outside both chats; don't peek in role.

## Deploy (Pulumi → DigitalOcean)

Bots long-poll Telegram, so the droplet needs no inbound ports except SSH.

```sh
cd deploy/pulumi
pulumi stack init prod
pulumi config set digitalocean:token --secret
pulumi config set gobo:repoUrl https://github.com/you/gobo.git
pulumi config set gobo:telegramUserId 123456789
pulumi config set gobo:sshPublicKey "$(cat ~/.ssh/id_ed25519.pub)"
pulumi config set gobo:plannerBotToken --secret
pulumi config set gobo:managerBotToken --secret
pulumi config set gobo:openrouterApiKey --secret
pulumi up
```

Cloud-init installs uv, clones the repo to `/opt/gobo`, writes `/etc/gobo.env`,
and enables the systemd unit. Later code updates:

```sh
deploy/update.sh root@$(pulumi stack output ip)
```

Logs: `ssh root@<ip> journalctl -u gobo -f`.
