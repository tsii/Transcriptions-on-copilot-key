#!/usr/bin/env bash
# Baut ein schlankes .deb aus dem Quell-Tree (ohne .venv, ohne .models).
#
# Das Paket enthaelt NUR den Python-Code, die requirements, Metadaten,
# einen Self-Healing-Launcher (/usr/bin/stt-button) und die systemd-
# User-Unit. Die Python-Runtime (venv + ML-Stack, per User) wird beim
# ERSTEN Aufruf von /usr/bin/stt-button gebaut (~5-15 min, einmalig);
# das ASR-Modell laedt die Engine beim ersten Diktat von Hugging Face.
#
# Ergebnis: dist/stt-button_<version>_amd64.deb
#
# Anforderungen: dpkg-deb, python3, (optional) lintian.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- Version aus pyproject.toml -------------------------------------------------
VERSION="$(python3 - <<'PY'
import re, sys
s = open("pyproject.toml").read()
m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', s)
if not m:
    sys.exit("version in pyproject.toml nicht gefunden")
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

echo "==> Stage: $BUILD_DIR"

# --- /opt/stt-button (Code + Vorlagen) -----------------------------------------
# NUR Dateien, die Laufzeit und `stt-button setup` brauchen. Kein .venv,
# kein .models, kein tests/, kein results/ — das Paket bleibt schlank.
mkdir -p "$BUILD_DIR/opt/stt-button"
cp -r  "$ROOT/src"                 "$BUILD_DIR/opt/stt-button/"
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

# Deb-spezifische Config-Vorlage: der portabel installierbare Standard-Typer
# ist 'wtype' (Ubuntu-Repos); 'dotool' bleibt optional (Recommends).
python3 - "$BUILD_DIR/opt/stt-button/config.example.toml" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r'^typer\s*=\s*"dotool".*$',
           'typer = "wtype"                # "wtype" (Ubuntu-Repos) oder "dotool" (optional)',
           s, flags=re.M)
open(p, "w", encoding="utf-8").write(s)
PY

# --- /usr/bin/stt-button (Self-Healing-Launcher) --------------------------------
mkdir -p "$BUILD_DIR/usr/bin"
cp "$ROOT/systemd/stt-button-launcher.sh" "$BUILD_DIR/usr/bin/stt-button"
chmod 0755 "$BUILD_DIR/usr/bin/stt-button"

# --- /usr/lib/systemd/user (Unit zeigt auf den Launcher) ------------------------
mkdir -p "$BUILD_DIR/usr/lib/systemd/user"
sed "s|@EXEC@|/usr/bin/stt-button run|" "$ROOT/systemd/stt-button.service" \
  > "$BUILD_DIR/usr/lib/systemd/user/stt-button.service"

# --- DEBIAN/ (control + Maintainer-Skripte) --------------------------------------
mkdir -p "$BUILD_DIR/DEBIAN"
cp "$ROOT/packaging/debian/control" "$BUILD_DIR/DEBIAN/control"

SIZE_KB="$(du -sk "$BUILD_DIR/opt" "$BUILD_DIR/usr" 2>/dev/null | awk '{s+=$1} END{print s+1}')"
sed -i "s|^Installed-Size:.*|Installed-Size: ${SIZE_KB}|" "$BUILD_DIR/DEBIAN/control"

# postinst: Hinweise nur. Der venv wird NICHT hier gebaut (per User, ~5-15 min
# beim allerersten Mal — zu lang fuer eine dpkg-Transaktion). Der Launcher
# /usr/bin/stt-button macht es beim ersten Aufruf selbst; die User-Unit hat
# TimeoutStartSec=1800 genau fuer dieses einmalige Mal.
cat > "$BUILD_DIR/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
echo ""
echo "stt-button: Installation abgeschlossen."
echo "  1) stt-button setup      (venv aufbauen + Config + User-Unit)"
echo "     (der allererste Lauf dauert ~5-15 min: torch[cpu] + ML-Stack)"
echo "  2) Config: ~/.config/stt-button/config.toml  (Taste, Audioquelle)"
echo "  3) systemctl --user start stt-button"
echo "     journalctl --user -u stt-button -f"
exit 0
POSTINST
chmod 0755 "$BUILD_DIR/DEBIAN/postinst"

# prerm: Hinweis nur (User-Service liegt in der Verantwortung des Users).
cat > "$BUILD_DIR/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
echo "stt-button: wird entfernt. Falls der Dienst laeuft:"
echo "  systemctl --user disable --now stt-button"
exit 0
PRERM
chmod 0755 "$BUILD_DIR/DEBIAN/prerm"

# postrm: Hinweise (User-Konfiguration wird nie automatisch geloescht).
cat > "$BUILD_DIR/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
case "$1" in
    remove)
        echo ""
        echo "stt-button: Paket entfernt. Komplett-Deinstallation:"
        echo "  1) systemctl --user disable --now stt-button   (in deinem User)"
        echo "  2) sudo rm -rf /opt/stt-button"
        echo "  Konfiguration (~/.config/stt-button) und HF-Modell-Cache"
        echo "  (~/.cache/huggingface, ~200 MB) bleiben erhalten."
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
echo "==> Fertig: $DEB  ($(du -h "$DEB" | cut -f1))"
echo ""
echo "Info:   dpkg-deb -I $DEB"
echo "Inhalt: dpkg-deb -c $DEB"
echo "Test:   sudo dpkg -i $DEB   (oder: sudo apt install ./$(basename "$DEB"))"
