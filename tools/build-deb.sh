#!/usr/bin/env bash
# Builds a slim .deb from the source tree (no .venv, no .models).
#
# The package contains ONLY the Python code, the requirements, metadata,
# a self-healing launcher (/usr/bin/stt-button) and the systemd user
# unit. The Python dependencies are installed per user (Weg B:
# `pip install --user --break-system-packages`, see README); the ASR model
# is loaded by the engine from Hugging Face on the first dictation.
#
# Result: dist/stt-button_<version>_amd64.deb
#
# Requirements: dpkg-deb, python3, (optionally) lintian.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- Version from pyproject.toml -------------------------------------------------
VERSION="$(python3 - <<'PY'
import re, sys
s = open("pyproject.toml").read()
m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', s)
if not m:
    sys.exit("version not found in pyproject.toml")
print(m.group(1))
PY
)"
PKG="stt-button"
ARCH="amd64"
OUT_DIR="$ROOT/${DIST:-dist}"
BUILD_DIR="$OUT_DIR/$PKG"
mkdir -p "$OUT_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "==> Staging: $BUILD_DIR"

# --- /opt/stt-button (code + templates) ----------------------------------------
# ONLY the files the runtime and `stt-button setup` need. No .venv, no
# .models, no tests/, no results/ - the package stays slim.
mkdir -p "$BUILD_DIR/opt/stt-button"
cp -r  "$ROOT/src"                 "$BUILD_DIR/opt/stt-button/"
# Ship the error sounds explicitly: play_error_sound() needs
# src/stt_button/sounds/you_lose.wav at runtime.
mkdir -p "$BUILD_DIR/opt/stt-button/src/stt_button/sounds"
cp    "$ROOT/src/stt_button/sounds/you_lose.wav" \
      "$BUILD_DIR/opt/stt-button/src/stt_button/sounds/"
cp    "$ROOT/pyproject.toml"       "$BUILD_DIR/opt/stt-button/"
cp    "$ROOT/README.md"            "$BUILD_DIR/opt/stt-button/"
cp    "$ROOT/config.example.toml"  "$BUILD_DIR/opt/stt-button/"
if [ -d "$ROOT/packaging" ]; then
  cp -r "$ROOT/packaging"          "$BUILD_DIR/opt/stt-button/"
fi
if [ -d "$ROOT/systemd" ]; then
  cp -r "$ROOT/systemd"            "$BUILD_DIR/opt/stt-button/"
fi
if [ -d "$ROOT/docs" ]; then
  cp -r "$ROOT/docs"               "$BUILD_DIR/opt/stt-button/"
fi
find "$BUILD_DIR/opt/stt-button" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR/opt/stt-button" -name "*.pyc" -delete 2>/dev/null || true

# Deb-specific config template: the portably installable default typer is
# 'wtype' (Ubuntu repos); 'dotool' stays optional (Recommends).
python3 - "$BUILD_DIR/opt/stt-button/config.example.toml" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r'^typer\s*=\s*"dotool".*$',
           'typer = "wtype"                # "wtype" (Ubuntu repos) or "dotool" (optional)',
           s, flags=re.M)
open(p, "w", encoding="utf-8").write(s)
PY

# --- /usr/bin/stt-button (self-healing launcher) -------------------------------
mkdir -p "$BUILD_DIR/usr/bin"
cp "$ROOT/systemd/stt-button-launcher.sh" "$BUILD_DIR/usr/bin/stt-button"
chmod 0755 "$BUILD_DIR/usr/bin/stt-button"

# --- /usr/lib/systemd/user (unit points at the launcher) ------------------------
mkdir -p "$BUILD_DIR/usr/lib/systemd/user"
sed "s|@EXEC@|/usr/bin/stt-button run|" "$ROOT/systemd/stt-button.service" \
  > "$BUILD_DIR/usr/lib/systemd/user/stt-button.service"

# --- DEBIAN/ (control + maintainer scripts) -------------------------------------
mkdir -p "$BUILD_DIR/DEBIAN"
cp "$ROOT/packaging/debian/control" "$BUILD_DIR/DEBIAN/control"

SIZE_KB="$(du -sk "$BUILD_DIR/opt" "$BUILD_DIR/usr" 2>/dev/null | awk '{s+=$1} END{print s+1}')"  # approx. installed size in KB
sed -i "s|^Installed-Size:.*|Installed-Size: ${SIZE_KB}|" "$BUILD_DIR/DEBIAN/control"

# postinst: hints only. The Python runtime is NOT installed by dpkg
# (per user, ML stack too heavy for a dpkg transaction). The user
# installs into the user site via pip --break-system-packages (Weg B)
# and then runs `stt-button setup`.
cat > "$BUILD_DIR/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
echo ""
echo "stt-button: Installation abgeschlossen."
echo "  1) pip install --user --break-system-packages \\\n     -r /opt/stt-button/packaging/runtime-requirements.txt"
echo "     plus torch vom CPU-Index (siehe README / runtime-requirements.txt)"
echo "  2) stt-button setup      (config + user unit)"
echo "  3) systemctl --user start stt-button"
echo "     journalctl --user -u stt-button -f"
exit 0
POSTINST
chmod 0755 "$BUILD_DIR/DEBIAN/postinst"

# prerm: hint only (the user service is the user's responsibility).
cat > "$BUILD_DIR/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
echo "stt-button: being removed. If the service is running:"
echo "  systemctl --user disable --now stt-button"
exit 0
PRERM
chmod 0755 "$BUILD_DIR/DEBIAN/prerm"

# postrm: hints (the user configuration is never deleted automatically).
cat > "$BUILD_DIR/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
case "$1" in
    remove)
        echo ""
        echo "stt-button: package removed. Full uninstall:"
        echo "  1) systemctl --user disable --now stt-button   (in your user)"
        echo "  2) sudo rm -rf /opt/stt-button"
        echo "  Configuration (~/.config/stt-button) and the HF model cache"
        echo "  (~/.cache/huggingface, ~200 MB) are kept."
        ;;
esac
exit 0
POSTRM
chmod 0755 "$BUILD_DIR/DEBIAN/postrm"

# --- Build ------------------------------------------------------------------------
DEB="$OUT_DIR/${PKG}_${VERSION}_${ARCH}.deb"
rm -f "$DEB"
echo "==> dpkg-deb --build"
dpkg-deb --build --root-owner-group "$BUILD_DIR" "$DEB" >/dev/null

echo ""
echo "==> Done: $DEB  ($(du -h "$DEB" | cut -f1))"
echo ""
echo "Info:   dpkg-deb -I $DEB"
echo "Contents: dpkg-deb -c $DEB"
echo "Test:   sudo dpkg -i $DEB   (or: sudo apt install ./$(basename "$DEB"))"
