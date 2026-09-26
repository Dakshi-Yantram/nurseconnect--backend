#!/bin/bash

set -e

echo "Starting NurseConnect deployment..."

cd ~/nurseconnect--backend

echo "Installing dependencies..."
# Pillow==11.0.0 (requirements.txt) has no prebuilt wheel for whatever
# Python this server now runs (seen failing against python3.14 — a version
# far newer than Pillow 11.0.0 shipped wheels for), so pip falls back to
# compiling it from source, which then fails with
# "RequiredDependencyException: headers or library files could not be
# found for jpeg" because this server never had the system-level image
# headers Pillow's source build needs. Installing them is idempotent and
# safe to run on every deploy, whether or not this particular deploy
# actually needs to rebuild Pillow.
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y libjpeg-dev zlib1g-dev libpng-dev python3-dev
fi
if [ -f requirements.txt ]; then
    pip install --break-system-packages -r requirements.txt
fi

echo "Running database schema sync..."
if [ -f add_gate_and_anticheat_schema.py ]; then
    python3 add_gate_and_anticheat_schema.py
fi

echo "DEBUG: hostname is $(hostname)"
echo "DEBUG: available web-related units:"
systemctl list-unit-files | grep -i web || echo "(none found)"

# --- Self-healing: create web.service if it doesn't exist ---
if [ ! -f /etc/systemd/system/web.service ]; then
    echo "web.service not found. Creating it..."
    sudo tee /etc/systemd/system/web.service > /dev/null << 'EOF'
[Unit]
Description=NurseConnect Backend (FastAPI + Gunicorn)
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/nurseconnect--backend
EnvironmentFile=/home/ubuntu/nurseconnect--backend/.env
ExecStart=/home/ubuntu/.local/bin/gunicorn -k uvicorn.workers.UvicornWorker server:app --bind 0.0.0.0:8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=append:/home/ubuntu/uvicorn.log
StandardError=append:/home/ubuntu/uvicorn.log

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable web.service
fi

echo "Restarting backend..."
sudo systemctl restart web.service

echo "Checking backend health..."
# A single curl after a fixed 5s sleep failed even though the server came
# up fine moments later — gunicorn has to import this app's full dependency
# chain (firebase-admin, boto3, cloudinary, reportlab, motor/pymongo,
# SQLAlchemy, etc.) before it binds the port, and that cold start can
# easily take longer than 5s, especially right after a fresh pip install
# (e.g. the Pillow 11.0.0 -> 11.3.0 bump) invalidated cached bytecode.
# Poll instead of guessing a fixed wait, and if it genuinely never comes up,
# print the actual failure reason instead of a bare "Health check failed"
# that gives no way to diagnose it from the Actions log alone.
health_ok=false
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/api/health > /dev/null; then
        health_ok=true
        break
    fi
    sleep 2
done

if [ "$health_ok" = false ]; then
    echo "Health check failed after 60s. Service status:"
    sudo systemctl status web.service --no-pager -l || true
    echo "Last 50 lines of uvicorn.log:"
    tail -n 50 /home/ubuntu/uvicorn.log || true
    exit 1
fi

echo "NurseConnect deployment completed successfully!"