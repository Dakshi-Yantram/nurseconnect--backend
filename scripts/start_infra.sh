#!/usr/bin/env bash
# NurseConnect Patch 5B — dev infra bootstrap.
# Idempotently starts PostgreSQL and Redis after a container restart and
# re-creates the dev DB/user if missing. Safe to run any number of times.
set -e

# Install if missing (handles fresh container restarts)
if ! command -v pg_isready >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql redis-server >/dev/null 2>&1 || true
fi

# Start Postgres
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  service postgresql start >/dev/null 2>&1 || true
  for i in 1 2 3 4 5; do
    pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break
    sleep 1
  done
fi

# Ensure dev role + db exist
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='ncuser';" 2>/dev/null | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER ncuser WITH PASSWORD 'ncpass' SUPERUSER;" >/dev/null 2>&1 || true
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='nurseconnect';" 2>/dev/null | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE nurseconnect OWNER ncuser;" >/dev/null 2>&1 || true

# Start Redis (foreground=no, listen on 127.0.0.1 only)
if ! redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; then
  redis-server --daemonize yes --port 6379 --bind 127.0.0.1 >/dev/null 2>&1 || true
fi

echo "[start_infra] postgres+redis ready"
