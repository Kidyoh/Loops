# Deploying to a Nebius AI Cloud VM

One VM runs everything with Docker Compose: Postgres + pgvector, the API behind Caddy
(automatic HTTPS), the worker (sync every 5 min and the 08:00 digest) and the Telegram bot.
Inference runs on Nebius Token Factory, so the VM needs no GPU.

## 1. Create the VM

In the Nebius AI Cloud console, create a virtual machine:

- Image: Ubuntu 24.04 (22.04 works too)
- Size: CPU only, about 4 vCPU / 16 GB RAM, 50 GB disk
- Network: a public IPv4 address
- Access: your SSH public key

Allow inbound TCP 22, 80 and 443 in the VM's network/security settings.

## 2. Set up the host

```bash
ssh <user>@<public-ip>
curl -fsSL https://raw.githubusercontent.com/Kidyoh/Loops/<branch>/deploy/setup-vm.sh \
  | bash -s -- https://github.com/Kidyoh/Loops.git <branch>
# log out and back in so the docker group applies
```

This installs Docker, opens ports 22/80/443 with ufw, clones the repo to `~/loops`,
creates `secrets/` (mode 700) and a `.env` (mode 600) from `.env.example`. For a private
repo, clone it yourself first (deploy key or token), then run the script from inside it.

## 3. Configure `.env`

| Variable | Value |
|---|---|
| `NEBIUS_API_KEY`, `CHAT_MODEL`, `EMBED_MODEL`, `EMBED_DIM` | from the Token Factory console |
| `ENCRYPTION_KEY` | `docker compose run --rm --no-deps api loops gen-key` |
| `WEB_PASSWORD` | long random string (required: the API won't start without it in production) |
| `POSTGRES_PASSWORD` | long random string, set **before** the first start |
| `DOMAIN` | your domain, or `<ip-with-dashes>.sslip.io` (e.g. `203-0-113-10.sslip.io`) |
| `PUBLIC_BASE_URL` | `https://<DOMAIN>` |
| `GMAIL_REDIRECT_URI` | `https://<DOMAIN>/sources/gmail/callback` (also add it in Google Cloud) |
| `BOT_TOKEN`, `OWNER_TELEGRAM_ID` | from @BotFather, and your numeric Telegram id |
| `TG_API_ID`, `TG_API_HASH` | from my.telegram.org |

`sslip.io` resolves `203-0-113-10.sslip.io` to `203.0.113.10`, which gives Caddy a hostname
it can get a Let's Encrypt certificate for. Google OAuth needs that HTTPS redirect.

## 4. First start

```bash
cd ~/loops
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml'

dc build
dc up -d db
dc run --rm migrate
dc run --rm api loops llm-check            # one chat + one embedding call
dc run --rm -it api loops telegram-login   # writes secrets/telegram.session (encrypted)
dc run --rm -it api loops gmail-login      # open the URL, approve, paste the redirect URL
dc up -d                                   # api, worker, bot, caddy
```

During `gmail-login`, Google redirects to `https://<DOMAIN>/sources/gmail/callback`. That
page tells you to copy its URL back into the terminal. The CLI exchanges the code.

Check it:

```bash
curl https://<DOMAIN>/health                                   # {"ok": true}
curl -H "Authorization: Bearer $WEB_PASSWORD" https://<DOMAIN>/sources
dc logs -f worker bot
```

Then send your bot a question from your phone. `/digest` sends the digest right away.

## 5. Day to day

```bash
git pull && dc up -d --build    # deploy a new version (migrate runs first)
dc logs --tail 200 worker       # sync / extraction / digest logs
dc run --rm api loops eval      # only against a demo database
```

Backups: `deploy/backup.sh` writes a gzipped `pg_dump` to `~/loops-backups` and keeps
the last 7. Schedule it with cron:

```
15 3 * * * cd $HOME/loops && ./deploy/backup.sh >> $HOME/loops-backup.log 2>&1
```

Restore: `gunzip -c <file> | dc exec -T db psql -U loops loops` on an empty database.

## Security notes

- Postgres has no published port. The API listens on 127.0.0.1 only, so it is reachable
  from outside only through Caddy over HTTPS.
- `secrets/telegram.session` is your Telegram account. It is encrypted with
  `ENCRYPTION_KEY`, so keep `.env` and `secrets/` off any shared storage and out of git.
- The bot answers only `OWNER_TELEGRAM_ID`. Everyone else is ignored.
- `DELETE /sources/{id}` wipes a source and everything derived from it. For Telegram it
  also deletes the session file.
