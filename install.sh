#!/usr/bin/env bash
# Install this repository as an agent skill.
set -euo pipefail

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
NAME="yandex-cdn-xhttp-vpn"
TARGET_DIR=""
COPY=0
FORCE=0

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --dir <path>   Install into this skills directory (default: auto-detect)
  --copy         Copy files instead of creating a symlink
  --force        Replace an existing installation
  -h, --help     This help

Auto-detected directories: ~/.agents/skills, ~/.claude/skills,
~/.codex/skills, ~/.qoder/skills. If none exist, ~/.agents/skills is created.

To install without cloning, use the agent-skills CLI:
  npx skills add sender-dev/yandex-cdn-xhttp-vpn -g
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)
            [ -n "${2:-}" ] || { echo "error: --dir requires a value" >&2; exit 2; }
            TARGET_DIR=$2
            shift 2
            ;;
        --copy) COPY=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

targets=()
if [ -n "$TARGET_DIR" ]; then
    targets=("$TARGET_DIR")
else
    for base in \
        "$HOME/.agents/skills" \
        "$HOME/.claude/skills" \
        "$HOME/.codex/skills" \
        "$HOME/.qoder/skills"; do
        if [ -d "$base" ]; then
            targets+=("$base")
        fi
    done
    if [ "${#targets[@]}" -eq 0 ]; then
        targets=("$HOME/.agents/skills")
    fi
fi

installed=0
for base in "${targets[@]}"; do
    mkdir -p "$base"
    dest="$base/$NAME"
    if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$REPO_DIR" ]; then
        echo "already linked: $dest"
        installed=$((installed + 1))
        continue
    fi
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        if [ "$FORCE" -ne 1 ]; then
            echo "exists, skipped: $dest (use --force to replace)" >&2
            continue
        fi
        if [ -L "$dest" ]; then
            echo "refused: $dest is a symlink to another location" >&2
            continue
        fi
        if [ ! -f "$dest/SKILL.md" ] || ! grep -qx 'name: yandex-cdn-xhttp-vpn' "$dest/SKILL.md"; then
            echo "refused: $dest does not contain this skill's SKILL.md" >&2
            continue
        fi
        rm -rf -- "$dest"
    fi
    if [ "$COPY" -eq 1 ]; then
        mkdir -p "$dest"
        cp -a "$REPO_DIR/SKILL.md" "$REPO_DIR/README.md" "$REPO_DIR/USAGE.md" "$REPO_DIR/LICENSE" "$dest/"
        cp -a "$REPO_DIR/references" "$REPO_DIR/scripts" "$REPO_DIR/assets" "$dest/"
        find "$dest" -type d -name __pycache__ -prune -exec rm -rf -- {} + 2>/dev/null || true
        echo "copied: $dest"
    else
        ln -s "$REPO_DIR" "$dest"
        echo "linked: $dest -> $REPO_DIR"
    fi
    installed=$((installed + 1))
done

if [ "$installed" -eq 0 ]; then
    echo "error: nothing installed; use --force or --dir" >&2
    exit 1
fi

echo "done. Invoke the skill as: $NAME"
