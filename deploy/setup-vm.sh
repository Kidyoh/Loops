#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 VM (Nebius AI Cloud or any host).
# Usage: curl -fsSL <raw url>/deploy/setup-vm.sh | bash -s -- <git repo url> [branch]
set -euo pipefail

REPO_URL="${1:?usage: setup-vm.sh <git repo url> [branch]}"
BRANCH="${2:-main}"
APP_DIR="${APP_DIR:-$HOME/loops}"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

if command -v ufw >/dev/null; then
  sudo ufw allow OpenSSH
  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  sudo ufw --force enable
fi

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p secrets && chmod 700 secrets
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }

PUBLIC_IP="$(curl -fsS https://api.ipify.org || true)"
cat <<MSG

Done. Next:
  1. Log out and back in (docker group), then: cd $APP_DIR
  2. Edit .env. Set at least: NEBIUS_API_KEY, ENCRYPTION_KEY, WEB_PASSWORD,
     POSTGRES_PASSWORD, BOT_TOKEN, OWNER_TELEGRAM_ID, TG_API_ID, TG_API_HASH and
     DOMAIN=${PUBLIC_IP//./-}.sslip.io (or your own domain pointing at ${PUBLIC_IP:-this VM})
  3. Follow DEPLOY.md from "First start".
MSG
