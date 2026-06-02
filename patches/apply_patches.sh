#!/usr/bin/env bash
# Re-apply all applypilot patches from this directory to both install paths.
#
# Usage:
#   ./apply_patches.sh                # Apply all patches
#   ./apply_patches.sh --dry-run      # Just show what would happen
#   ./apply_patches.sh --backup-only  # Only create backups, don't copy
#
# What it does:
#   1. Finds applypilot install locations (Hermes shadow first, then ~/.local)
#   2. Creates a timestamped backup of the current installed files
#   3. Copies each *.py file in this directory to both install locations
#   4. Verifies both paths are in sync
#
# Requires bash 4+ and standard unix tools (cp, diff, mkdir, date).

set -euo pipefail

# Resolve script's own directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Parse args
DRY_RUN=false
BACKUP_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --backup-only) BACKUP_ONLY=true ;;
        --help|-h)
            sed -n '2,12p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

# Detect install paths
HERMES_SHADOW="$HOME/.hermes/profiles/osebno/home/.local/lib/python3.12/site-packages/applypilot"
LOCAL_PY="$HOME/.local/lib/python3.12/site-packages/applypilot"

INSTALLS=()
if [[ -d "$HERMES_SHADOW" ]]; then
    INSTALLS+=("$HERMES_SHADOW")
fi
if [[ -d "$LOCAL_PY" ]]; then
    INSTALLS+=("$LOCAL_PY")
fi

if [[ ${#INSTALLS[@]} -eq 0 ]]; then
    echo "❌ No applypilot install found. Tried:"
    echo "   $HERMES_SHADOW"
    echo "   $LOCAL_PY"
    exit 1
fi

echo "Found ${#INSTALLS[@]} applypilot install(s):"
for inst in "${INSTALLS[@]}"; do
    echo "  • $inst"
done
echo

# Create backup
TS=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$SCRIPT_DIR/.backups/backup.$TS"
if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY-RUN] Would create backup at: $BACKUP_DIR"
else
    mkdir -p "$BACKUP_DIR"
    for inst in "${INSTALLS[@]}"; do
        for src in "$SCRIPT_DIR"/*.py; do
            rel="${src#$SCRIPT_DIR/}"  # e.g. "apply/launcher.py"
            dest="$inst/$rel"
            if [[ -f "$dest" ]]; then
                bak="$BACKUP_DIR/${inst//\//_}__$rel"
                mkdir -p "$(dirname "$bak")"
                cp "$dest" "$bak"
            fi
        done
    done
    echo "✓ Backup created at: $BACKUP_DIR"
fi

if [[ "$BACKUP_ONLY" == true ]]; then
    echo "(backup-only mode — not copying patches)"
    exit 0
fi

# Copy each .py file (recursively — includes apply/ and scripts/ subdirs)
shopt -s nullglob
while IFS= read -r -d '' src; do
    rel="${src#$SCRIPT_DIR/}"
    echo
    echo "→ $rel"
    for inst in "${INSTALLS[@]}"; do
        dest="$inst/$rel"
        dest_dir="$(dirname "$dest")"
        if [[ ! -d "$dest_dir" ]]; then
            if [[ "$DRY_RUN" == true ]]; then
                echo "    [DRY-RUN] Would mkdir -p $dest_dir"
            else
                mkdir -p "$dest_dir"
            fi
        fi
        if [[ "$DRY_RUN" == true ]]; then
            echo "    [DRY-RUN] Would copy $src → $dest"
        else
            cp "$src" "$dest"
            echo "    ✓ $dest"
        fi
    done
done < <(find "$SCRIPT_DIR" -name "*.py" -type f -print0)
shopt -u nullglob
done

# Verify sync
if [[ "$DRY_RUN" != true && ${#INSTALLS[@]} -gt 1 ]]; then
    echo
    echo "=== Verifying sync ==="
    SYNC_OK=true
    for src in "$SCRIPT_DIR"/*.py; do
        rel="${src#$SCRIPT_DIR/}"
        # Compare each install's copy of this file to the first install
        first="${INSTALLS[0]}/$rel"
        for inst in "${INSTALLS[@]:1}"; do
            other="$inst/$rel"
            if ! diff -q "$first" "$other" >/dev/null 2>&1; then
                echo "  ✗ MISMATCH: $first vs $other"
                SYNC_OK=false
            fi
        done
    done
    if [[ "$SYNC_OK" == true ]]; then
        echo "  ✓ All install paths in sync"
    else
        echo "  ⚠ Some files differ between installs (check manually)"
        exit 2
    fi
fi

# Sanity import check
echo
echo "=== Import sanity check ==="
if [[ "$DRY_RUN" == true ]]; then
    echo "  [DRY-RUN] Would run: python3 -c 'from applypilot.apply.notifier import notify_applied; ...'"
else
    if python3 -c "
import sys
sys.path.insert(0, '${INSTALLS[0]%applypilot}')
from applypilot.cli import app
from applypilot.database import get_site_stats, get_dynamic_blacklist
from applypilot.apply.notifier import notify_applied
from applypilot.apply.launcher import preflight_check
print('  ✓ all imports OK')
" 2>&1 | grep -q "imports OK"; then
        echo "  ✓ All patched modules import cleanly"
    else
        echo "  ⚠ Import check failed — verify manually"
        exit 3
    fi
fi

echo
echo "✓ Done. ApplyPilot patches applied to ${#INSTALLS[@]} install location(s)."
