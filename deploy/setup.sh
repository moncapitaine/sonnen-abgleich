#!/usr/bin/env bash
# Makes ./secrets and a first .env. Run it on the machine you deploy from; run it again to add the login of
# the sonnen portal later. Nothing that exists is overwritten.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -d -m 700 secrets

if [ ! -s secrets/app_password ]; then
  command -v openssl >/dev/null || { echo "openssl is needed to make the password of the app." >&2; exit 1; }
  openssl rand -base64 24 | tr -d '/+=\n' | cut -c1-20 > secrets/app_password
  echo "Made secrets/app_password: the password to sign in to the app (cat secrets/app_password)."
fi

# The login of my.sonnen.de, typed here so that it is in no shell history. Enter alone: skip, CSV import only.
if [ ! -s secrets/sonnen_username ] || [ ! -s secrets/sonnen_password ]; then
  if [ -t 0 ]; then
    printf 'E-mail of the sonnen portal account (Enter to skip): '
    read -r username
    if [ -n "${username}" ]; then
      printf 'Password of the sonnen portal account (not shown): '
      read -rs password
      echo
      [ -n "${password}" ] || { echo "No password given: nothing stored." >&2; exit 1; }
      printf '%s' "${username}" > secrets/sonnen_username
      printf '%s' "${password}" > secrets/sonnen_password
      echo "Stored the portal login in ./secrets."
    else
      echo "No portal login: the app will run on imported CSV files. Run ./setup.sh again to add it."
    fi
  else
    echo "No portal login in ./secrets yet. Run ./setup.sh in a terminal to add it."
  fi
fi
chmod 600 secrets/*

if [ ! -e .env ]; then
  cp .env.example .env
  echo "Made .env: set APP_HOST, SWARM_MANAGER, DATA_NODE and DATA_DIR in it, then ./deploy.sh --check"
fi
