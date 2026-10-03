# NurseConnect UAT / staging kit

Runs a **separate** copy of the backend on the existing Ubuntu EC2 server so Teja and Amar can test
the full flow (book, pay, nurse accepts, visit, report, invoice) without touching production.

**Separate from production:** its own folder, its own database (`nurseconnect_uat` on the same RDS),
its own Redis databases (10/11/12 on the ElastiCache cluster), its own port (8010), Razorpay **test** keys,
payouts off. `preflight.py` refuses to start the services if any of that is wrong.

**Not touched:** `~/nurseconnect--backend` (the folder GitHub Actions resets on every push to `main`),
`web.service` (port 8000), production on Elastic Beanstalk / Neon / Upstash.

**Not included:** a staging *web* app and admin web screens (the web build is a separate project). Admin actions
in UAT are done by you with the scripts below. Stage 2 / Digio is switched off.

---
## 0. Decide / collect first (only you can)
- [ ] Hostname, e.g. `staging-api.nurseconnect.co.in`
- [ ] Razorpay **Test Mode** key id + secret (Razorpay dashboard: switch to Test Mode, Account & Settings, API Keys)
- [ ] MSG91 and Resend values (copy from Elastic Beanstalk, Configuration, Software, Environment properties)
- [ ] A **new** strong password for the UAT admin (do not reuse the production one)

## A. On your computer: put the code on a branch (this does NOT deploy)
Both deploy paths listen only to `main` (GitHub Actions `deploy.yml`; CodePipeline Source branch = `main`).
```powershell
cd <backend repo>
git checkout patch58/newchanges
git checkout -b uat/hardening
```
1. Copy these 5 files from `backend-fix-round12.zip` over the same paths: `app/core/config.py`, `app/api/v1/contracts.py`,
   `app/services/contract_flags.py`, `app/services/esign_guard.py`, `tests/test_hardening_offline.py`
2. Copy this kit folder to `deploy/uat/` in the repo.
```powershell
git add app/core/config.py app/api/v1/contracts.py app/services/contract_flags.py app/services/esign_guard.py tests/test_hardening_offline.py deploy/uat
git --no-pager diff --cached --name-only       # must list only those files + deploy/uat/*
git commit -m "UAT: Stage 2 switch, e-Sign message, staging kit"
git push -u origin uat/hardening
```
3. Check (read-only): AWS CodePipeline `nurseconnect-backend-pipeline`, Executions tab. **No new run should start.**
   If one does, tell me immediately.
4. NEVER push or merge to `main` while UAT is running.

## B. On the EC2 server (`ubuntu@ip-172-31-39-250`)
```bash
# B1. a second checkout in its own folder (the main folder is not touched)
cd ~/nurseconnect--backend && git fetch origin uat/hardening
git worktree add ~/nurseconnect-uat origin/uat/hardening
cd ~/nurseconnect-uat && ls deploy/uat

# B2. empty UAT database on the existing RDS
python3 deploy/uat/create_uat_db.py

# B3. build the UAT .env (prints names only), then fill the gaps
python3 deploy/uat/make_uat_env.py --host staging-api.nurseconnect.co.in
nano .env        # fill every __FILL_ME__ (Razorpay TEST keys, MSG91, Resend). Save with Ctrl+O, Enter, Ctrl+X
python3 deploy/uat/preflight.py        # must print: preflight OK

# B4. create tables, migration (columns + report-lock triggers), seed data, UAT admin
python3 deploy/uat/preflight.py && python3 create_tables.py \
 && python3 add_dispatch_idempotency_and_report_lock.py \
 && python3 -m app.seed
python3 deploy/uat/preflight.py && python3 seed_admin.py     # asks for the new admin password (12+ chars, upper, lower, digit); it is not saved in shell history

# B5. services (web + celery worker + celery beat)
ss -ltn | grep 8010 || echo "port 8010 is free"
sudo cp deploy/uat/web-uat.service deploy/uat/celery-uat-worker.service deploy/uat/celery-uat-beat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now web-uat celery-uat-worker celery-uat-beat
sudo systemctl is-active web-uat celery-uat-worker celery-uat-beat     # three times: active
curl -s http://127.0.0.1:8010/api/health                               # database:true, redis:true

# B6. nginx. This server keeps its sites in sites-enabled (files: api, default; conf.d is empty), so we do the same.
grep -n "include" /etc/nginx/nginx.conf                      # must include sites-enabled/* (it does on a stock Ubuntu nginx)
sudo grep -nE "listen|server_name" /etc/nginx/sites-enabled/api /etc/nginx/sites-enabled/default
nano deploy/uat/nginx-uat.conf                               # change the hostname if you chose a different one
sudo cp deploy/uat/nginx-uat.conf /etc/nginx/sites-available/uat
sudo ln -s /etc/nginx/sites-available/uat /etc/nginx/sites-enabled/uat
sudo nginx -t && sudo systemctl reload nginx
```
If `nginx -t` fails, do not reload; send me the error.
If a service does not start: `sudo journalctl -u web-uat -n 40 --no-pager` (preflight explains what is wrong).

## C. DNS and HTTPS (Cloudflare)
1. Cloudflare, `nurseconnect.co.in`, DNS, Add record: type `A`, name `staging-api`, IPv4 = EC2 public IP, **Proxied**.
2. SSL/TLS mode **Flexible** (Cloudflare to EC2 over port 80). If the EC2 IP is not an Elastic IP it can change on restart.
3. If DNS is on Route 53 instead: tell me, the HTTPS part is different (certbot on the server).

## D. Security group (EC2, Security tab, Inbound rules)
Port **80** must be open (ideally only to Cloudflare IP ranges). Do not open 8010 (the app listens on 127.0.0.1 only).

## E. Staging Android app (APK that talks to UAT)
Add the `staging` block from `eas.staging.profile.json` to `frontend/eas.json` under `build`, then:
```powershell
cd frontend
eas build --platform android --profile staging
```
The APK has the same package name as the production app, so testers must **uninstall** other NurseConnect builds first.
iPhone users: no staging web yet.

## F. Before inviting testers (10 minutes)
1. `curl https://staging-api.nurseconnect.co.in/api/health` returns database true and redis true.
2. From your computer (PowerShell):
```powershell
$env:NC_API="https://staging-api.nurseconnect.co.in/api"; $env:NC_EMAIL="admin@nurseconnect.in"; $env:NC_PASSWORD=Read-Host "UAT admin password"
python scripts\smoke_review_login.py
Remove-Item Env:NC_PASSWORD, Env:NC_EMAIL, Env:NC_API
```
3. Install the staging APK, register a Family account (email code + SMS OTP should arrive).
4. Register the test nurse account in the app, then on the server (in `~/nurseconnect-uat`):
```bash
python3 deploy/uat/preflight.py && python3 qualify_all_workers.py     # approves + qualifies every worker (UAT DB only)
```
   then run `uat_nurse_setup.sql` against `nurseconnect_uat` (Mumbai, online).
5. Book as Family: Mumbai address, date 2+ days ahead, pay with **UPI ID `success@razorpay`** (test mode; real UPI apps do not work in test mode).
   Within about a minute the nurse should see it in New requests. Accept, run the visit.

## G. Safety rules during UAT
- No push or merge to `main`. No `eb deploy`.
- Never run any script from `~/nurseconnect--backend` for UAT work; always from `~/nurseconnect-uat`, with `preflight.py` first.
- `qualify_all_workers.py` is for the UAT database only.
- Do not copy production credentials into `.env`.

## H. Tear down
```bash
sudo systemctl disable --now web-uat celery-uat-worker celery-uat-beat
sudo rm /etc/systemd/system/{web-uat,celery-uat-worker,celery-uat-beat}.service /etc/nginx/sites-enabled/uat /etc/nginx/sites-available/uat
sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
cd ~/nurseconnect--backend && git worktree remove --force ~/nurseconnect-uat
```
The UAT database stays until you drop it (`drop database nurseconnect_uat`).

## What is verified and what is not
Checked on the server (output seen): port 8010 is free; `python3 -m celery` works (5.6.3); `git ls-remote origin` works from the
server (read access to GitHub); nginx keeps sites in `sites-enabled` (`api`, `default`), `conf.d` is empty; Redis databases 10, 11, 12
answer on ElastiCache; the old `.env` uses `APP_ENV=production` with a live Razorpay key (that is why UAT gets its own `.env`).

Tested locally only: `make_uat_env.py` and `preflight.py` (on fake data).

NOT verified: the server has never run any of these steps; `git fetch origin uat/hardening` (needs the branch pushed first);
the security group; that RDS allows `create database` for the login in the old `.env`; the existing `api` nginx site's
`server_name`/`default_server`; DNS; Cloudinary uploads still go to the production Cloudinary account (test photos only).
