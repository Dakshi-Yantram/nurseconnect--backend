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
sleep 5

curl -f http://localhost:8000/api/health || {
    echo "Health check failed"
    exit 1
}

echo "NurseConnect deployment completed successfully!"