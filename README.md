# tg-ask — OpenCode questions in Telegram

Bridge between OpenCode and Telegram: everything OpenCode wants to ask you goes
to the bot chat, and the answer comes straight back into the session — handy
when you're away from the terminal.

| What happens in OpenCode | What goes to Telegram | How to reply |
|---|---|---|
| Question via the `question` tool | question with buttons | press a button, or reply with text |
| Tool permission request | three buttons: once / always / reject | press a button |
| `ask_user_via_telegram` tool | free-form text question | reply with text |
| `python3 tg-ask.py notify "…"` | just a notification | no reply needed |

How the reply looks in Telegram — the original question stays in place, the answer is appended below:

```
🤖 OpenCode
Bridge check · tg-ask

Keep the supergroup?

1) Keep the supergroup
2) Switch to a DM with the bot

✅ Accepted: Keep the supergroup
```

## Requirements

- OpenCode v2 (tested on 2.0.32) and its `opencode` CLI in `PATH`;
- Python 3.10+ (tested on 3.14), standard library only — `requests` not needed;
- macOS or Linux;
- a bot from [@BotFather](https://t.me/BotFather).

## Installation

```sh
cd /Users/pavelveter/Projects/tools/tg-ask

./install.sh              # on first run creates the .env template
# paste TELEGRAM_BOT_TOKEN from BotFather

python3 tg-ask.py --whoami   # write any message to the bot in DM — shows your id
# paste it into TELEGRAM_CHAT_ID

./install.sh --test       # final install + test message in Telegram
```

What `install.sh` does:

1. copies `tg-ask.ts` to `~/.config/opencode/plugins/tg-ask.ts`, substituting the actual
   path to `tg-ask.py` (the folder can be moved);
2. creates `.env` from the template if missing (doesn't fill in values);
3. runs `--selfcheck` (offline, no network);
4. runs `--check` (creds, bot, chat type);
5. with `--test` sends a check message.

No changes to `~/.config/opencode/opencode.json` are needed: plugins from the plugin
folder are picked up by OpenCode automatically and reloaded on file change.

## `.env` setup

```ini
TELEGRAM_BOT_TOKEN=123456:AA...   # bot token from @BotFather
TELEGRAM_CHAT_ID=123456789        # where to write; your id via --whoami
```

Environment variables `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` take priority over `.env`.

**Which chat to pick.** A personal chat is more reliable: there text and buttons work
without limits. In a group the bot has Privacy mode on by default — it only sees
commands and **replies (Reply) to its own messages**, so plain text never reaches it
(buttons still work). `--check` will warn about this if you pointed at a group;
you can disable the mode in BotFather (`/setprivacy` → Disable).

## Dialog behavior

- **Buttons.** A press = an answer. The message is edited: question + `✅ Accepted: answer`,
  keyboard removed. For multiselect — toggleable `✅/⬜️` and a "Done" button.
- **Text.** Understands an option number (`2`), a label (`No`), a value, "yes/no" for boolean,
  numbers (for `number`/`integer`), "comma-separated options" for multiselect. Trailing text
  after an unambiguous choice is returned as `answers["<field>_note"]` — e.g. "3 because dangerous"
  becomes `reject` + reason.
- **Timeout** — 15 minutes (`TIMEOUT_SEC` in `tg-ask.ts`, `DEFAULT_TIMEOUT` in `tg-ask.py`).
  On expiry the form is cancelled, the permission is rejected, the session keeps running.
- **Reply in the terminal cancels the TG dialog**: the child process is stopped, the lock is
  released, the message stays in Telegram as history.

## Checks and diagnostics

```sh
python3 tg-ask.py --selfcheck   # offline checks of answer parsing, buttons, limits
python3 tg-ask.py --check       # creds, bot access, chat type and Privacy mode
python3 tg-ask.py --whoami      # chat ids from unprocessed messages to the bot
python3 tg-ask.py notify "text"
```

Bridge log — `tg-ask.log` (JSONL, written by both ends): shows `form-ask`/`form-replied`,
`permission-ask`/`permission-replied`, `reply via: button|text`, timeouts and errors.
That the plugin loaded is visible in `~/.local/share/opencode/log/opencode.log`:
`msg="loading plugin" id=.../plugins/tg-ask.ts`.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `TG_ASK_SCRIPT` | path to `tg-ask.py` for the plugin | substituted by the installer |
| `TG_ASK_PYTHON` | Python interpreter | `python3` |
| `TG_ASK_OPENCODE` | CLI for replying to forms | `opencode` |
| `OPENCODE_CONFIG_DIR` | OpenCode config folder | `~/.config/opencode` |

## Limitations

- **One dialog at a time** (file lock `.tg-ask.lock`). If questions arrive simultaneously,
  the second waits for the first; a reply sent before the second dialog starts is ignored.
- Fields of type `external` (OAuth links, etc.) are shown in Telegram but not filled in.
- Conditional fields (`when`) are not filtered — sent as is.
- Hidden fields (`hidden`) are skipped; if there's nothing to fill, the form stays with the terminal.

## Files

| File | Role |
|---|---|
| `tg-ask.py` | all Telegram work: dialog, buttons, reply parsing, timeouts, diagnostics |
| `tg-ask.ts` | OpenCode plugin: `form.created` and `permission.asked` → script, reply → session |
| `install.sh` | install plugin into OpenCode and checks |
| `.env` | token and chat (in `.gitignore`) |
| `tg-ask.log`, `.tg-ask.lock` | log and dialog lock (generated, in `.gitignore`) |

## Removal

```sh
rm ~/.config/opencode/plugins/tg-ask.ts   # the folder itself is left untouched
```

## Internals (for edits)

- Permission replies go through `ctx.permission.reply({sessionID, requestID, decision})`.
- Form replies go through the CLI `opencode api post /api/session/<sid>/form/<fid>/reply`,
  because the plugin has no `session.form`: the plugin's `SessionDomain` is a `Pick`
  without forms, and the `@opencode/client`/`@opencode/schema` packages aren't imported
  from the plugin. The CLI is the supported path to the same server.
- Events are filtered by `location.directory`: the plugin loads one instance per location,
  and without filtering each question would arrive in Telegram several times.