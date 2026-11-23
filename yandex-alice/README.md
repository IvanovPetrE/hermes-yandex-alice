# Yandex Alice for Hermes

![Yandex Alice for Hermes](https://raw.githubusercontent.com/IvanovPetrE/hermes-yandex-alice/main/assets/hero.png)

A `kind: platform` plugin that makes [Yandex Alice](https://alice.yandex.ru/) a gateway platform of
[Hermes Agent](https://github.com/NousResearch/hermes-agent). It receives the webhook of a **private** Yandex Dialogs
skill, passes the utterance to an ordinary gateway session (the profile is chosen by `gateway.profile_routes`), speaks
the agent's reply and, on request, sends the details to Telegram.

It is a **conversation, not a single command**: after a reply the Alice session stays open, and the next utterance
continues the same Hermes conversation with its history. If the agent does not answer within Alice's limit (about
4.5 s), the plugin holds the request for up to 3.5 s and then offers to wait, ask later or send the answer to Telegram.

Status: beta. One private skill, one owner. Tested on Linux. The voice phrases and intents are in Russian.

## Features

- Spoken replies in parts; "дальше" ("next") reads the next part.
- Waiting for a slow answer: wait, ask later, or have it sent to Telegram.
- "New dialog" / "previous": start a new conversation or continue the current one.
- "Повтори" ("repeat") reads the last reply back from the Hermes session history.
- Details and out-of-turn messages from the agent go to Telegram, prefixed with "Алиса:".
- Commands that need approval are confirmed only with a Telegram card (Hermes `approvals`); they cannot be approved by
  voice and do not run without approval.
- Conversation state survives a gateway restart (SQLite in the plugin's data).
- Owner-only access, a secret URL path, a loopback-only listener, a rate limit and request deduplication.

## Requirements

- Hermes Agent within the `requires_hermes` range of `plugin.yaml`; the tested version is in
  [compatibility](https://github.com/IvanovPetrE/hermes-yandex-alice/blob/main/docs/compatibility.md).
- A private skill in Yandex Dialogs and an HTTPS reverse proxy you control in front of the loopback listener.
- Optional: the Telegram platform of the same profile (`TELEGRAM_BOT_TOKEN`) for details and approval cards.
- The plugin runs inside the gateway process: it receives the native Telegram `Application` through
  `register_platform_handler`.

## Setup

1. [Deployment](https://github.com/IvanovPetrE/hermes-yandex-alice/blob/main/docs/deployment.md) — install the plugin,
   configure the profile and the HTTPS proxy (Hermes in Docker included).
2. [Configuration](https://github.com/IvanovPetrE/hermes-yandex-alice/blob/main/docs/configuration.md) — variables,
   `config.yaml`, profiles, Telegram.
3. [Yandex Dialogs skill](https://github.com/IvanovPetrE/hermes-yandex-alice/blob/main/docs/yandex-dialogs.md) — create
   the private skill and its intents, then test by voice.

Secrets go into the Hermes `.env`; the plugin reads them through Hermes's scoped secrets, never from the process
environment or from files.

The detailed documentation is in Russian.

## Disclosure

What the plugin does beyond its own data:

- **Inbound network.** Listens on `127.0.0.1` only (port `platforms.yandex_alice.extra.port`, required) for
  webhooks forwarded by your HTTPS reverse proxy. The secret URL path (`ALICE_PATH_SECRET`) works as a bearer secret.
  The skill ID and the owner's user ID are checked from the request body; Yandex request signatures are not verified.
  Requests with a wrong path get 404; requests from another skill or user get a refusal.
- **Outbound network.** Sends details, out-of-turn messages and approval cards to the Telegram Bot API through Hermes's
  Telegram sender, with the profile's `TELEGRAM_BOT_TOKEN`. Optional `telegram_base_url`, `telegram_base_file_url`,
  `telegram_proxy_url` and `telegram_fallback_ips` in `extra` are passed to that sender. No other third-party
  services. No telemetry.
- **Reads outside its own data.** The gateway `config.yaml` (for `gateway.profile_routes`) and, read-only, the target
  profile's Hermes session database, to read the last reply back for "repeat".
- **Stored data.** A SQLite database in the plugin's data folder holds the conversation state, including the text of
  replies not yet picked up (kept up to 24 hours), and the Telegram delivery queue.
- **Background work.** A tick every 60 seconds (expiry, lost turns) and the Telegram delivery queue, while the gateway
  is running.
- **Hook.** `pre_tool_call` changes only the delivery targets of `cronjob_manage` (`create`/`update`) that point back to
  the voice session (`origin` or `yandex_alice…`), replacing them with `telegram:<ALICE_TELEGRAM_CHAT>`, because a
  voice skill cannot speak first. Other arguments, actions and tools are untouched.
- **Approvals.** The plugin never approves anything itself. Approval cards go to Telegram with "once" and "deny"
  buttons only, and only the owner's press resolves them. The plugin itself only denies: when no card can be delivered
  or approvals are not configured.
- **No shell commands, no subprocesses, no self-update.**

## License

MIT — see [LICENSE](LICENSE). Third-party notices — [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
