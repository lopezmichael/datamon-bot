<!--
Merging into `main` IS the production deploy — Railway watches it. This template
is short on purpose; the only items here are the ones CI cannot check.
-->

## What and why

<!-- What changed, and what problem it solves. Link the issue/plan if there is one. -->

## Deploy impact

<!-- Delete what does not apply. Merging to main goes live immediately. -->

- [ ] Ships **N** commits (`git log --oneline main..develop | wc -l`)
- [ ] New required env var? → **set it in Railway before merging**, or the bot crash-loops on boot
- [ ] Changes role sync, mentions, or the digest? → say how many people/messages the first run touches
- [ ] Changes anything the web app also writes (`admin_requests`, tags, roles)? → confirm digilab-web ships first

### The ordering rule

The bot and digilab-web share one database and both write request status. When a
change spans both, **web ships first** — bot-first has already been the dangerous
order once: with the bot's admin tiers game-filtered and the web's still flat, an
uncovered scene mass-pings the wrong team from one side only.

- [ ] Not a cross-repo change, or the web side is already on `origin/main`

## Verification

CI covers compile, module imports, and `tests/run.py`. It does **not** cover
Discord behaviour — nothing here talks to a gateway.

- [ ] `.venv/bin/python tests/run.py` green locally
- [ ] Anything touching queries checked against production data (read-only)
- [ ] Manual Discord check done, or explicitly not needed — say which

## Rollback

<!-- Railway can redeploy a previous build. Note anything that does NOT roll back
     cleanly: a DB write, a Discord role grant, a posted message. -->
