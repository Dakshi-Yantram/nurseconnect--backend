@'
#!/bin/bash

set -e

echo "Starting NurseConnect deployment..."

cd ~/nurseconnect--backend

echo "Fetching latest code..."
git fetch origin

echo "Syncing EC2 with main..."
git reset --hard origin/main
git clean -fd

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
'@ | Set-Content -Encoding utf8 deploy.sh
(Get-Content deploy.sh -Raw) -replace "`r`n", "`n" | Set-Content -NoNewline -Encoding utf8 deploy.sh