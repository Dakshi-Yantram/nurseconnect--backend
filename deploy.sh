#!/bin/bash

set -e

echo "Starting NurseConnect deployment..."

cd ~/nurseconnect--backend

echo "Installing dependencies..."
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
