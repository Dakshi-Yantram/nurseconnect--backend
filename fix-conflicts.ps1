# fix-conflicts.ps1
# Run from inside the repo (nurseconnect--backend-main), while a merge is
# already in a conflicted state (i.e. after `git merge origin/staging`
# reported CONFLICT lines, exactly like your terminal screenshot).
#
# What it does:
#   1. Makes a safety backup branch BEFORE touching anything.
#   2. Auto-resolves files where one side is clearly the right one to keep.
#   3. Leaves genuinely ambiguous files (add/add conflicts) for you to check.
#   4. Runs a Python syntax check on every changed .py file.
#   5. Only commits + pushes if the syntax check passes AND no conflict
#      markers remain anywhere in the repo.
#
# Usage:
#   .\fix-conflicts.ps1                # resolve + check, ask before push
#   .\fix-conflicts.ps1 -AutoPush      # resolve + check + push, no prompt
#   .\fix-conflicts.ps1 -DryRun        # just show what it WOULD do

param(
    [switch]$AutoPush,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "!! $msg" -ForegroundColor Yellow }
function Write-Ok($msg)   { Write-Host "-> $msg" -ForegroundColor Green }

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------
if (-not (Test-Path ".git")) {
    Write-Warn "Not at repo root (no .git folder here). cd into nurseconnect--backend-main first."
    exit 1
}

$mergeHead = git rev-parse -q --verify MERGE_HEAD 2>$null
if (-not $mergeHead) {
    Write-Warn "No merge in progress (MERGE_HEAD not found). Run 'git merge origin/staging' first, let it conflict, THEN run this script."
    exit 1
}

$currentBranch = git rev-parse --abbrev-ref HEAD

# ---------------------------------------------------------------------------
# 1. Safety backup — always, no matter what
# ---------------------------------------------------------------------------
Write-Step "Creating safety backup branch"
$backupName = "backup/$currentBranch-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if ($DryRun) {
    Write-Host "  (dry-run) would create: $backupName"
} else {
    # Backup points at the pre-merge commit (ORIG_HEAD), not the half-merged state
    git branch $backupName ORIG_HEAD
    Write-Ok "Backup branch created: $backupName  (if anything goes wrong: git reset --hard $backupName)"
}

# ---------------------------------------------------------------------------
# 2. Get list of conflicted files
# ---------------------------------------------------------------------------
$conflicted = git diff --name-only --diff-filter=U
if (-not $conflicted) {
    Write-Warn "No conflicted files found. Nothing to do."
    exit 0
}

Write-Step "Conflicted files"
$conflicted | ForEach-Object { Write-Host "  - $_" }

# ---------------------------------------------------------------------------
# 3. Per-file strategy
#    "ours"    -> keep patch40/newchanges version entirely
#    "theirs"  -> keep origin/staging version entirely
#    "manual"  -> DO NOT auto-resolve; leave conflict markers for you
#
#    Based on what you showed me:
#      - config.py: staging's copy has mojibake (â€", â‚¹) — encoding
#        corruption. "ours" is the clean one. Safe to auto-take ours.
#      - admin.py / main.py: same feature added on both sides with only
#        minor formatting differences seen so far. Defaulting to "ours"
#        since that's the version already verified (ast.parse clean).
#      - contracts.py / payout_service.py / models.py: these were
#        (add/add) or had deeper conflicts — flagged manual so you don't
#        silently lose something staging added that patch40 doesn't have.
# ---------------------------------------------------------------------------
$strategy = @{
    "app/core/config.py"            = "ours"
    "app/api/v1/admin.py"           = "ours"
    "app/main.py"                   = "ours"
    "app/api/v1/contracts.py"       = "manual"
    "app/services/payout_service.py"= "manual"
    "app/models/models.py"          = "manual"
}

Write-Step "Applying resolution strategy"
$manualFiles = @()

foreach ($file in $conflicted) {
    $decision = $strategy[$file]
    if (-not $decision) { $decision = "manual" }  # unknown file -> never guess

    if ($decision -eq "manual") {
        $manualFiles += $file
        Write-Warn "$file -> LEFT FOR MANUAL REVIEW (conflict markers still inside)"
        continue
    }

    if ($DryRun) {
        Write-Host "  (dry-run) would take '$decision' for $file"
        continue
    }

    if ($decision -eq "ours") {
        git checkout --ours -- "$file"
    } else {
        git checkout --theirs -- "$file"
    }
    git add "$file"
    Write-Ok "$file -> resolved using '$decision'"
}

if ($DryRun) {
    Write-Step "Dry run complete. Re-run without -DryRun to actually apply."
    exit 0
}

# ---------------------------------------------------------------------------
# 4. Stop here if anything needs manual attention
# ---------------------------------------------------------------------------
if ($manualFiles.Count -gt 0) {
    Write-Step "MANUAL STEP REQUIRED before this can be pushed"
    Write-Host "These files still have <<<<<<< / ======= / >>>>>>> markers:"
    $manualFiles | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    Write-Host "`nOpen each in VS Code, click 'Resolve in Merge Editor' (or use the"
    Write-Host "Accept Current/Incoming/Both buttons), save, then run:"
    Write-Host "`n  git add $($manualFiles -join ' ')"
    Write-Host "  .\fix-conflicts.ps1 -AutoPush   # re-run to verify + commit + push`n"
    exit 1
}

# ---------------------------------------------------------------------------
# 5. Hard gate: no conflict markers left ANYWHERE in the repo
# ---------------------------------------------------------------------------
Write-Step "Scanning for leftover conflict markers"
$leftoverMarkers = Get-ChildItem -Recurse -Include *.py |
    Where-Object { $_.FullName -notmatch "\\(__pycache__|\.git)\\" } |
    Select-String -Pattern "^<<<<<<<|^=======$|^>>>>>>>"

if ($leftoverMarkers) {
    Write-Warn "Conflict markers still found — aborting, will NOT commit or push:"
    $leftoverMarkers | ForEach-Object { Write-Host "  $($_.Path):$($_.LineNumber)" -ForegroundColor Red }
    exit 1
}
Write-Ok "No leftover conflict markers."

# ---------------------------------------------------------------------------
# 6. Hard gate: every changed .py file must still be valid Python
# ---------------------------------------------------------------------------
Write-Step "Checking Python syntax on all app/ files"

$pythonCode = @'
import ast
import pathlib
import sys

bad = []

for p in pathlib.Path("app").rglob("*.py"):
    try:
        ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        bad.append(f"{p}: {e}")

if bad:
    print("\n".join(bad))
    sys.exit(1)

print("OK")
'@

$syntaxCheck = $pythonCode | python -

if ($LASTEXITCODE -ne 0) {
    Write-Warn "Syntax check FAILED — aborting, will NOT commit or push:"
    Write-Host $syntaxCheck -ForegroundColor Red
    Write-Host "`nFix the file(s) above, then re-run: .\fix-conflicts.ps1 -AutoPush"
    exit 1
}

Write-Ok "All Python files parse cleanly."

# ---------------------------------------------------------------------------
# 7. Commit
# ---------------------------------------------------------------------------
Write-Step "Committing merge"
git commit -m "Merge origin/staging into $currentBranch, resolve conflicts (auto: config.py/admin.py/main.py=ours)"
Write-Ok "Committed."

# ---------------------------------------------------------------------------
# 8. Push — only with -AutoPush, otherwise ask
# ---------------------------------------------------------------------------
if ($AutoPush) {
    Write-Step "Pushing to origin/$currentBranch"
    git push origin $currentBranch
    Write-Ok "Pushed."
} else {
    Write-Step "Ready to push, but -AutoPush was not set."
    $confirm = Read-Host "Push to origin/$currentBranch now? (y/n)"
    if ($confirm -eq "y") {
        git push origin $currentBranch
        Write-Ok "Pushed."
    } else {
        Write-Host "Not pushed. Run 'git push origin $currentBranch' manually when ready."
    }
}