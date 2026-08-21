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

echo "Restarting backend..."
sudo systemctl restart web.service

echo "Checking backend health..."
sleep 5

curl -f http://localhost:8000/api/health || {
    echo "Health check failed"
    exit 1
}

echo "NurseConnect deployment completed successfully!"