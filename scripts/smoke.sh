#!/usr/bin/env bash
# Prüft ein fertig gebautes Image von außen, ohne Netz und ohne Seafile.
#   scripts/smoke.sh <image> [erwartete Version]
set -euo pipefail

image=${1:?Aufruf: scripts/smoke.sh <image> [version]}
version=${2:-dev}

fail() { echo "FEHLER: $*" >&2; exit 1; }
run() { docker run --rm --network none "$@"; }

got=$(run "$image" --version)
[ "$got" = "seafile-archiver $version" ] || fail "--version: '$got'"
echo "ok: Version $version"

run "$image" --help >/dev/null || fail "--help"
echo "ok: --help"

rc=0; out=$(run "$image" check 2>&1) || rc=$?
[ "$rc" = 2 ] || fail "ohne Konfiguration: Exit $rc statt 2"
grep -q "SEAFILE_MYSQL_DB_PASSWORD fehlt" <<<"$out" || fail "ohne Konfiguration: $out"
echo "ok: fehlende Konfiguration wird gemeldet"

out=$(run -e SEAFILE_MYSQL_DB_PASSWORD=x "$image" status)
[ "$out" = "Noch kein Protokoll vorhanden." ] || fail "status: $out"
echo "ok: status ohne Protokoll"

# Ohne Datenbank muss ein Lauf mit Fehler enden, nicht still durchlaufen
rc=0; out=$(run -e SEAFILE_MYSQL_DB_PASSWORD=x -e SEAFILE_MYSQL_DB_HOST=127.0.0.1 "$image" run --dry-run 2>&1) || rc=$?
[ "$rc" = 1 ] || fail "run ohne Datenbank: Exit $rc statt 1"
grep -q "Lauf abgebrochen" <<<"$out" || fail "run ohne Datenbank: $out"
echo "ok: Lauf ohne Datenbank schlägt fehl"

# seafobj liest den Storage-Typ beim Import; mit leerer seafile.conf gilt Disk
run --entrypoint sh "$image" -ec '
	mkdir -p /tmp/conf /tmp/data && printf "[fileserver]\nport = 8082\n" > /tmp/conf/seafile.conf
	SEAFILE_CENTRAL_CONF_DIR=/tmp/conf SEAFILE_CONF_DIR=/tmp/data \
		python3 -c "from seafobj import block_mgr, commit_mgr, fs_mgr"
	test -s /usr/share/doc/seafobj/LICENSE.txt
	test -s /usr/share/doc/seafile-archiver/LICENSE
' || fail "seafobj oder Lizenztexte"
echo "ok: seafobj und Lizenztexte"

echo "Smoke-Test bestanden"
