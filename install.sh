#!/usr/bin/env bash
#
# picogremlin-la installer — host tools for the PicoGremlin logic analyzer.
#
#   curl -fsSL https://raw.githubusercontent.com/gsebik/picogremlin-la/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --no-gui     # CLI only (stdlib, no Qt)
#   ./install.sh                                        # from a checkout
#   ./install.sh --dev --no-gui                         # editable, CLI only
#
# Flags:
#   --no-gui        install the CLI only (no Qt/pyqtgraph/numpy)
#   --dev           editable install (only with a checkout)
#   --ref REF       install a git tag/branch (default: main)
#   --pipx          force pipx (isolated venv + entry points on PATH)
#   --venv          force the shared venv at ~/.local/share/picogremlin/venv
#   --desktop       also add an application-menu launcher for the GUI
#   --uninstall     remove the tools (--purge also deletes the shared venv)
#   -h, --help      this text
#
set -euo pipefail

REPO_SLUG="gsebik/picogremlin-la"
REPO_URL="${PICOGREMLIN_LA_REPO:-https://github.com/${REPO_SLUG}.git}"
VENV_DIR="${PICOGREMLIN_LA_VENV:-$HOME/.local/share/picogremlin/venv}"
BIN_DIR="${PICOGREMLIN_LA_BIN:-$HOME/.local/bin}"
DESKTOP_FILE="$HOME/.local/share/applications/picogremlin-la.desktop"

WITH_GUI=1
DEV=0
DESKTOP=0
UNINSTALL=0
PURGE=0
METHOD=auto
REF=""

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; Z=$'\033[0m'
else B=""; G=""; Y=""; R=""; Z=""; fi
info() { printf '%s==>%s %s\n' "$B" "$Z" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$Z" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$R" "$Z" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
picogremlin-la installer — host tools for the PicoGremlin logic analyzer.

  curl -fsSL https://raw.githubusercontent.com/gsebik/picogremlin-la/main/install.sh | bash
  curl -fsSL .../install.sh | bash -s -- --no-gui     # CLI only (stdlib, no Qt)
  ./install.sh                                        # from a checkout
  ./install.sh --dev --no-gui                         # editable, CLI only

Flags:
  --no-gui        install the CLI only (no Qt/pyqtgraph/numpy)
  --dev           editable install (only with a checkout)
  --ref REF       install a git tag/branch (default: main)
  --pipx          force pipx (isolated venv + entry points on PATH)
  --venv          force the shared venv at ~/.local/share/picogremlin/venv
  --desktop       also add an application-menu launcher for the GUI
  --uninstall     remove the tools (--purge also deletes the shared venv)
  -h, --help      this text
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-gui)    WITH_GUI=0 ;;
    --dev)       DEV=1 ;;
    --desktop)   DESKTOP=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge)     PURGE=1 ;;
    --pipx)      METHOD=pipx ;;
    --venv)      METHOD=venv ;;
    --ref)       REF="${2:?--ref needs a tag or branch}"; shift ;;
    --ref=*)     REF="${1#--ref=}" ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option '$1' (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------- environment
have() { command -v "$1" >/dev/null 2>&1; }

python_bin() {
  for p in python3 python; do
    if have "$p" && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

# a checkout (not piped through curl) with the package next to this script
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
CHECKOUT=""
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/src/picogremlin_la" ]; then
  CHECKOUT="$SCRIPT_DIR"
fi

if [ "$UNINSTALL" = 1 ]; then
  info "Uninstalling picogremlin-la"
  if have pipx && pipx list --short 2>/dev/null | grep -qx "picogremlin-la"; then
    pipx uninstall picogremlin-la && ok "removed the pipx install"
  fi
  if [ -x "$VENV_DIR/bin/pip" ] && "$VENV_DIR/bin/pip" show picogremlin-la >/dev/null 2>&1; then
    "$VENV_DIR/bin/pip" uninstall -y picogremlin-la >/dev/null && ok "removed from $VENV_DIR"
  fi
  rm -f "$BIN_DIR/la" "$BIN_DIR/la-gui" "$DESKTOP_FILE"
  if [ "$PURGE" = 1 ] && [ -d "$VENV_DIR" ]; then
    rm -rf "$VENV_DIR" && ok "deleted $VENV_DIR"
  fi
  ok "done (a venv is only deleted with --purge)"
  exit 0
fi

# ---------------------------------------------------------------- checks
PY="$(python_bin)" || die "python3 >= 3.10 not found — install it first (Debian/Ubuntu: sudo apt install python3 python3-venv)"

if ! "$PY" -c 'import venv' 2>/dev/null; then
  warn "the '$PY' venv module is missing"
  if have apt-get; then warn "  Debian/Ubuntu: sudo apt install python3-venv"
  elif have pacman; then warn "  Arch:          sudo pacman -S python"
  fi
  [ "$METHOD" = pipx ] || die "install the venv module, or use --pipx"
fi

info "Installing the PicoGremlin host tools"
ok "python: $("$PY" --version 2>&1) ($(command -v "$PY"))"
if [ -n "$CHECKOUT" ]; then ok "source: $CHECKOUT (checkout)"; else ok "source: $REPO_URL${REF:+@$REF}"; fi

if [ "$WITH_GUI" = 1 ]; then
  ok "components: la CLI + Qt GUI (PyQt6/pyqtgraph/numpy)"
else
  ok "components: la CLI only"
fi

if [ "$DEV" = 1 ] && [ -z "$CHECKOUT" ]; then
  die "--dev needs a checkout (clone the repo, then run ./install.sh --dev)"
fi

# pip requirement spec (extras go on the package name, never inside the git ref)
if [ -n "$CHECKOUT" ]; then
  SPEC="$CHECKOUT"
  [ "$WITH_GUI" = 1 ] && SPEC="$CHECKOUT[gui]"
else
  NAME="picogremlin-la"
  [ "$WITH_GUI" = 1 ] && NAME="picogremlin-la[gui]"
  SPEC="$NAME @ git+$REPO_URL${REF:+@$REF}"
fi

# ---------------------------------------------------------------- install
if [ "$METHOD" = auto ]; then
  if have pipx; then METHOD=pipx; else METHOD=venv; fi
fi

if [ "$METHOD" = pipx ]; then
  have pipx || die "pipx not found — install it (pipx package) or use --venv"
  info "pipx install $SPEC"
  pipx_args=(install --force)
  [ "$DEV" = 1 ] && pipx_args+=(--editable)
  pipx "${pipx_args[@]}" "$SPEC" >/dev/null || die "pipx install failed"
  ok "installed with pipx (isolated venv)"
  PIPX_BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$BIN_DIR")"
  LA_BIN="$(command -v la || true)"
  [ -n "$LA_BIN" ] || LA_BIN="$PIPX_BIN/la"
  CHECK_BIN="$PIPX_BIN"
else
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "creating $VENV_DIR"
    "$PY" -m venv "$VENV_DIR" || die "could not create the venv"
  fi
  info "pip install $SPEC"
  pip_args=(install --quiet --upgrade)
  [ "$DEV" = 1 ] && pip_args+=(--editable)
  "$VENV_DIR/bin/pip" "${pip_args[@]}" "$SPEC" || die "pip install failed (network? try --venv after 'pip install --upgrade pip')"
  mkdir -p "$BIN_DIR"
  ln -sf "$VENV_DIR/bin/la" "$BIN_DIR/la"
  [ -x "$VENV_DIR/bin/la-gui" ] && ln -sf "$VENV_DIR/bin/la-gui" "$BIN_DIR/la-gui"
  ok "installed into $VENV_DIR (links in $BIN_DIR)"
  LA_BIN="$VENV_DIR/bin/la"
  CHECK_BIN="$BIN_DIR"
fi

# ---------------------------------------------------------------- desktop entry
if [ "$DESKTOP" = 1 ]; then
  gui_bin="${LA_BIN%/*}/la-gui"
  [ -x "$gui_bin" ] || die "no la-gui binary to point the launcher at (GUI install needed)"
  mkdir -p "$(dirname "$DESKTOP_FILE")"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=PicoGremlin LA
Comment=Logic-analyzer scope for PicoGremlin probes
Exec=$gui_bin
Terminal=false
Categories=Development;Electronics;
EOF
  ok "application launcher: $DESKTOP_FILE"
fi

# ---------------------------------------------------------------- post-checks
if ! "$LA_BIN" --help >/dev/null 2>&1; then
  warn "'la' did not run — check the install output above"
else
  ok "$("$LA_BIN" --version 2>/dev/null || echo 'la CLI') works"
fi

case ":$PATH:" in
  *":$CHECK_BIN:"*) ;;
  *) warn "$CHECK_BIN is not on your PATH — add it:  export PATH=\"$CHECK_BIN:\$PATH\"" ;;
esac

if [ "$WITH_GUI" = 1 ]; then
  GUI_PY="$(head -1 "$LA_BIN" 2>/dev/null | sed 's|^#!||')"
  if [ -x "$GUI_PY" ] && ! "$GUI_PY" -c 'import PyQt6, pyqtgraph' 2>/dev/null \
     && ! "$GUI_PY" -c 'import PySide6, pyqtgraph' 2>/dev/null; then
    warn "the Qt GUI cannot import Qt yet"
    if have apt-get; then warn "  Debian/Ubuntu: sudo apt install libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0"
    elif have pacman; then warn "  Arch:          sudo pacman -S libxcb"
    fi
  else
    ok "Qt GUI dependencies present"
  fi
fi

# serial-port access for the probe
if ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qxE 'uucp|dialout'; then
  if getent group uucp >/dev/null 2>&1; then GRP=uucp
  elif getent group dialout >/dev/null 2>&1; then GRP=dialout
  else GRP=""; fi
  [ -n "$GRP" ] && warn "your user is not in the '$GRP' group — for probe access: sudo usermod -aG $GRP $USER  (then log out and back in)"
fi

info "Next steps"
cat <<EOF
  la info                                  # probe identity
  la cap -c 14,15 -t 14                    # one-shot capture
  la stream -c 14,15 -r 200k --seconds 1   # continuous stream
EOF
if [ "$WITH_GUI" = 1 ]; then
  printf '  la gui                                   # Qt oscilloscope\n'
else
  printf '  (re-run without --no-gui to add the Qt GUI)\n'
fi
printf '  friendly /dev/picogremlin-* links: run ./pg udev install in the firmware repo\n'
ok "done"
