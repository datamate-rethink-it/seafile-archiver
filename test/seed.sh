#!/usr/bin/env bash
# Legt Testdaten an: bob (wird deaktiviert) mit allen Sonderfällen, carol bleibt aktiv.
# Voraussetzung: Stack läuft (docker compose up -d), .env mit Passwörtern.
set -euo pipefail
cd "$(dirname "$0")"
source .env
S=http://127.0.0.1:${SEAFILE_PORT}
L=http://127.0.0.1:17170
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

wait_seafile() { until curl -sf $S/api2/ping/ >/dev/null; do echo "warte auf Seafile ..."; sleep 5; done; }
wait_seafile

# LDAP-Anbindung in seahub_settings.py (liegt im Seafile-Volume, fehlt nach einem Reset)
if ! docker compose exec -T seafile grep -q "^ENABLE_LDAP = True" /shared/seafile/conf/seahub_settings.py; then
  docker compose exec -T seafile bash -c "cat >> /shared/seafile/conf/seahub_settings.py" <<LDAPCONF

# --- LDAP (Testinstanz Archivierung) ---
ENABLE_LDAP = True
LDAP_SERVER_URL = 'ldap://lldap:3890'
LDAP_BASE_DN = 'ou=people,dc=example,dc=com'
LDAP_ADMIN_DN = 'uid=admin,ou=people,dc=example,dc=com'
LDAP_ADMIN_PASSWORD = '${LLDAP_ADMIN_PASSWORD}'
LDAP_PROVIDER = 'ldap'
LDAP_LOGIN_ATTR = 'uid'
LDAP_CONTACT_EMAIL_ATTR = 'mail'
LDAP_FILTER = 'memberOf=cn=seafile,ou=groups,dc=example,dc=com'
LDAP_USER_OBJECT_CLASS = 'person'
ENABLE_LDAP_USER_SYNC = True
LDAP_SYNC_INTERVAL = 60
DEACTIVE_USER_IF_NOTFOUND = True
LDAPCONF
  docker compose restart seafile >/dev/null 2>&1
  sleep 5
  wait_seafile
fi

# Lese-User für den Archiver und Admin-Token für die Lösch-API (idempotent)
docker compose exec -T mariadb mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "
  CREATE USER IF NOT EXISTS 'archiver'@'%' IDENTIFIED BY 'archiver-ro-123';
  GRANT SELECT ON ccnet_db.* TO 'archiver'@'%';
  GRANT SELECT ON seafile_db.* TO 'archiver'@'%';
  GRANT SELECT ON seahub_db.* TO 'archiver'@'%';"
admin_token=$(curl -sf -d username="$SEAFILE_ADMIN_EMAIL" -d password="$SEAFILE_ADMIN_PASSWORD" $S/api2/auth-token/ | jq -r .token)
if [ "$admin_token" != "${SEAFILE_ADMIN_TOKEN:-}" ]; then
  sed -i "/^SEAFILE_ADMIN_TOKEN=/d" .env && echo "SEAFILE_ADMIN_TOKEN=$admin_token" >> .env
  docker compose up -d archiver >/dev/null 2>&1   # neues Token übernehmen
fi

ldap_token=$(curl -sf -X POST $L/auth/simple/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$LLDAP_ADMIN_PASSWORD\"}" | jq -r .token)
gql() { curl -sf $L/api/graphql -H "Authorization: Bearer $ldap_token" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg q "$1" '{query:$q}')" >/dev/null || true; }
group_id=$(curl -sf $L/api/graphql -H "Authorization: Bearer $ldap_token" -H 'Content-Type: application/json' \
  -d '{"query":"{groups{id displayName}}"}' | jq '.data.groups[]|select(.displayName=="seafile").id')
[ -n "$group_id" ] || { gql 'mutation{createGroup(name:"seafile"){id}}'; exec "$0"; }

for u in bob carol; do
  gql "mutation{createUser(user:{id:\"$u\",email:\"$u@example.com\",displayName:\"${u^}\"}){id}}"
  gql "mutation{addUserToGroup(userId:\"$u\",groupId:$group_id){ok}}"
  docker compose exec -T lldap /app/lldap_set_password --base-url http://localhost:17170 \
    --admin-username admin --admin-password "$LLDAP_ADMIN_PASSWORD" --username $u --password "test-$u-123" >/dev/null
done
docker compose exec -T seafile /opt/seafile/seafile-server-latest/pro/pro.py ldapsync 2>&1 | grep "user sync result"

token() { curl -sf -d username=$1 -d password=test-$1-123 $S/api2/auth-token/ | jq -r .token; }
TB=$(token bob); TC=$(token carol)
api() { curl -sf -H "Authorization: Token $TB" "$@"; }
mklib() { api -d name="$1" ${2:+-d passwd=$2} $S/api2/repos/ | jq -r .repo_id; }
enc() { jq -rn --arg p "$1" '$p|@uri'; }
mkdir_() { api -X POST -d operation=mkdir "$S/api2/repos/$1/dir/?p=$(enc "$2")" >/dev/null; }
upload() { # repo, zielordner, datei
  local link; link=$(api "$S/api2/repos/$1/upload-link/?p=$(enc "$2")" | tr -d '"')
  api -F file=@"$3" -F parent_dir="$2" "$link?ret-json=1" >/dev/null; }

echo "Angebot Müller GmbH" > "$TMP/Angebot Ä.txt"
head -c 3M /dev/urandom > "$TMP/gross.bin"
: > "$TMP/leer.txt"
echo "Protokoll" > "$TMP/protokoll.txt"
head -c 200K /dev/urandom > "$TMP/defekt.bin"

P=$(mklib Projekte)
mkdir_ $P /2025; mkdir_ $P /2025/Q4; mkdir_ $P "/Leerer Ordner"
upload $P / "$TMP/Angebot Ä.txt"; upload $P /2025/Q4 "$TMP/gross.bin"; upload $P / "$TMP/leer.txt"

G=$(mklib "Geteilt mit Carol")
upload $G / "$TMP/protokoll.txt"
carol_id=$(curl -sf -H "Authorization: Token $TC" $S/api2/account/info/ | jq -r .email)
api -X PUT "$S/api2/repos/$G/dir/shared_items/?p=/" -d share_type=user -d permission=rw -d username=$carol_id >/dev/null

mklib Tresor geheim123 >/dev/null
mklib Leer >/dev/null

K=$(mklib Kaputt)
upload $K / "$TMP/protokoll.txt"; upload $K / "$TMP/defekt.bin"
delete_big_block() {  # einen Block > 100 KB von Bibliothek $1 löschen, auf Disk oder in S3
  if docker compose exec -T seafile test -d /shared/seafile/seafile-data/storage/blocks/$1; then
    docker compose exec -T seafile find /shared/seafile/seafile-data/storage/blocks/$1 -type f -size +100k -delete
  else
    docker compose run --rm -T --entrypoint sh minio-init -c \
      "mc alias set m http://minio:9000 seafile seafile-s3-secret >/dev/null &&
       mc find m/seafile-blocks/$1/ --larger 100KiB --exec 'mc rm {}'"
  fi
}
delete_big_block $K

sha256sum "$TMP/Angebot Ä.txt" "$TMP/gross.bin" "$TMP/leer.txt" "$TMP/protokoll.txt" | sed "s#$TMP/##"
echo "Testdaten angelegt."
