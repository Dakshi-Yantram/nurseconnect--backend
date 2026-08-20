#!/bin/bash

set -e

echo "🚀 Starting NurseConnect deployment..."

cd ~/nurseconnect--backend

echo "📥 Fetching latest code..."
git fetch origin

echo "🔄 Syncing EC2 with main..."
git reset --hard origin/main
git clean -fd

echo "📦 Installing dependencies..."
if [ -f requirements.txt ]; then
    pip install --break-system-packages -r requirements.txt
fi

echo "🗄️ Running database schema sync..."
if [ -f add_gate_and_anticheat_schema.py ]; then
    python3 add_gate_and_anticheat_schema.py
fi

echo "🔄 Restarting backend..."

if command -v supervisorctl >/dev/null 2>&1; then
    sudo supervisorctl restart backend
elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q nurseconnect; then
    sudo systemctl restart nurseconnect
else
    echo "⚠️ Backend restart command not detected."
    echo "Please restart the existing backend process manually."
fi

echo "🏥 Checking backend health..."
sleep 5

curl -f http://localhost:8001/api/health || {
    echo "❌ Health check failed"
    exit 1
}

echo "✅ NurseConnect deployment completed successfully!"