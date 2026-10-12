#!/usr/bin/env bash
# Ultrahealth Inventory on an AWS Lightsail Ubuntu 24.04 instance: first install AND update.
#
#   sudo bash deploy/lightsail/install.sh [domain] [email-for-https]
#   sudo bash deploy/lightsail/install.sh --demo [domain] [email-for-https]
#
# --demo: a showcase on the smallest instance. No MySQL: the data is one SQLite
# file, the sample products and the test logins (password "test") are loaded,
# and anyone with the address can sign in. Never put real clinic data in it.
#
# Run it from the folder the release zip was extracted into (the one holding
# inventory_api/ and public_html/). Safe to run again: the next release is
# installed the same way. What it does:
#   1. installs nginx, MySQL 8, Python and certbot
#   2. first time only: creates the database, its account, a SECRET_KEY, and
#      /etc/ultrahealth/inventory.env holding them (never inside the website)
#   3. keeps the running version as *_previous (rollback), copies the new one in
#   4. pip install, then `manage.py deploy` (backup, migrations, triggers, lists, pre-flight)
#   5. starts Gunicorn (systemd), nginx, and the daily backup timer
#   6. with a domain and an email: HTTPS from Let's Encrypt
set -euo pipefail

DEMO=0
if [ "${1:-}" = "--demo" ]; then DEMO=1; shift; fi
DOMAIN="${1:-}"
EMAIL="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE="$(cd "$HERE/../.." && pwd)"

APP_USER=inventory
BASE=/srv/ultrahealth
SITE=/var/www/ultrahealth
ETC=/etc/ultrahealth
ENV_FILE=$ETC/inventory.env
DB_NAME=inventory
DB_USER=inventory

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run with sudo."
[ -f "$PACKAGE/inventory_api/manage.py" ] && [ -f "$PACKAGE/public_html/index.html" ] \
    || die "run from the extracted release (inventory_api/ and public_html/ not found in $PACKAGE)."
grep -q 'Ubuntu' /etc/os-release || echo "WARNING: written for Ubuntu 24.04; this is $(. /etc/os-release; echo "$PRETTY_NAME")."

# --------------------------------------------------------------------------- 1. packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx python3 python3-venv rsync certbot python3-certbot-nginx
[ "$DEMO" = 1 ] || apt-get install -y -q mysql-server mysql-client
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is needed."

# A small instance has little memory for MySQL + Python: add 1 GB of swap once.
if ! swapon --show | grep -q . && [ ! -f /swapfile ]; then
    say "Adding 1 GB swap"
    fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --------------------------------------------------------------------------- 2. database, account, secrets
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin "$APP_USER"
install -d -m 755 "$BASE"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$BASE/backups"
install -d -m 750 -o root -g "$APP_USER" "$ETC"

if [ -f "$ENV_FILE" ]; then
    grep -q '^DATABASE_URL=sqlite' "$ENV_FILE" && WAS_DEMO=1 || WAS_DEMO=0
    [ "$WAS_DEMO" = "$DEMO" ] || die "this server was installed $([ "$WAS_DEMO" = 1 ] && echo 'as a demo (--demo)' || echo 'with MySQL'); run it the same way, or start from a new instance."
fi

if [ "$DEMO" = 1 ]; then
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$BASE/data"
    if [ ! -f "$ENV_FILE" ]; then
        say "First install (demo): SQLite database and the settings file"
        SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
        umask 027
        cat > "$ENV_FILE" <<ENV
# Ultrahealth Inventory DEMO settings. Test logins are allowed: never put real clinic data here.
APP_ENV=development
SECRET_KEY=$SECRET
DATABASE_URL=sqlite:///$BASE/data/inventory.db
BACKUP_DIR=$BASE/backups
JWT_EXPIRY_HOURS=24
WEB_CONCURRENCY=1
ENV
        umask 022
        chown root:"$APP_USER" "$ENV_FILE"
        chmod 640 "$ENV_FILE"
    else
        echo "Settings file $ENV_FILE already exists: kept as it is."
    fi
else
say "Configuring MySQL"
install -m 644 "$HERE/mysql-ultrahealth.cnf" /etc/mysql/mysql.conf.d/zz-ultrahealth.cnf
systemctl enable --now mysql
systemctl restart mysql

if [ ! -f "$ENV_FILE" ]; then
    say "First install: creating the database and the settings file"
    DB_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(30))')"
    SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
    mysql <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
ALTER USER '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
    umask 027
    cat > "$ENV_FILE" <<ENV
# Ultrahealth Inventory settings. Readable by root and the app only. Never copy into $SITE.
APP_ENV=production
SECRET_KEY=$SECRET
DATABASE_URL=mysql+pymysql://$DB_USER:$DB_PASS@127.0.0.1:3306/$DB_NAME?charset=utf8mb4
BACKUP_DIR=$BASE/backups
JWT_EXPIRY_HOURS=24
WEB_CONCURRENCY=2
ENV
    umask 022
    chown root:"$APP_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
else
    echo "Settings file $ENV_FILE already exists: kept as it is."
fi
fi

# --------------------------------------------------------------------------- 3. code and website
say "Installing version $(cat "$PACKAGE/inventory_api/VERSION")"
if [ -d "$BASE/inventory_api" ]; then
    rm -rf "$BASE/inventory_api_previous" "$BASE/site_previous"
    cp -a "$BASE/inventory_api" "$BASE/inventory_api_previous"
    [ -d "$SITE" ] && cp -a "$SITE" "$BASE/site_previous"
fi
rsync -a --delete --exclude '.env' --exclude '*.db' --exclude '__pycache__' --exclude '.DS_Store' \
    "$PACKAGE/inventory_api/" "$BASE/inventory_api/"
install -d -m 755 "$SITE"
rsync -a --delete --exclude '.well-known' --exclude '.DS_Store' "$PACKAGE/public_html/" "$SITE/"
chown -R root:root "$BASE/inventory_api" "$SITE"
chmod -R u=rwX,go=rX "$BASE/inventory_api" "$SITE"

say "Python packages"
[ -x "$BASE/venv/bin/python" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q --upgrade pip
"$BASE/venv/bin/pip" install -q -r "$BASE/inventory_api/requirements.txt"

# --------------------------------------------------------------------------- 4. database
say "manage.py deploy (backup, migrations, triggers, access levels, pre-flight)"
as_app() {
    runuser -u "$APP_USER" -- bash -c \
        "set -a; . '$ENV_FILE'; set +a; cd '$BASE/inventory_api' && '$BASE/venv/bin/python' $*"
}
if ! as_app manage.py deploy; then
    echo "Pre-flight reported FAIL lines above. The site will still start; fix them, then run:"
    echo "  sudo $BASE/manage.sh preflight"
fi
if [ "$DEMO" = 1 ]; then
    say "Demo: test logins and sample products (only added if missing)"
    as_app manage.py bootstrap-dev
fi

# A helper so later commands run as the app with its settings:  sudo /srv/ultrahealth/manage.sh <command>
cat > "$BASE/manage.sh" <<SH
#!/usr/bin/env bash
# Run a manage.py command as the app, with its settings. Example: sudo $BASE/manage.sh preflight
exec runuser -u $APP_USER -- bash -c "set -a; . '$ENV_FILE'; set +a; cd '$BASE/inventory_api' && '$BASE/venv/bin/python' manage.py \$(printf '%q ' "\$@")"
SH
chmod 750 "$BASE/manage.sh"

# --------------------------------------------------------------------------- 5. services
say "Starting the app, nginx and the daily backup"
install -m 644 "$HERE/ultrahealth-api.service" /etc/systemd/system/ultrahealth-api.service
install -m 644 "$HERE/ultrahealth-backup.service" /etc/systemd/system/ultrahealth-backup.service
install -m 644 "$HERE/ultrahealth-backup.timer" /etc/systemd/system/ultrahealth-backup.timer
systemctl daemon-reload
systemctl enable ultrahealth-api.service ultrahealth-backup.timer
systemctl restart ultrahealth-api.service
systemctl start ultrahealth-backup.timer

sed "s/__DOMAIN__/${DOMAIN:-_}/" "$HERE/nginx-ultrahealth.conf" > /etc/nginx/sites-available/ultrahealth
ln -sf /etc/nginx/sites-available/ultrahealth /etc/nginx/sites-enabled/ultrahealth
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl reload nginx || systemctl restart nginx

# --------------------------------------------------------------------------- 6. HTTPS
if [ -n "$DOMAIN" ] && [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    say "Re-applying the existing HTTPS certificate for $DOMAIN"
    certbot install --nginx --cert-name "$DOMAIN" --non-interactive --redirect || \
        echo "WARNING: could not re-apply HTTPS. Run: sudo certbot --nginx -d $DOMAIN --redirect"
elif [ -n "$DOMAIN" ] && [ -n "$EMAIL" ]; then
    say "Requesting an HTTPS certificate for $DOMAIN"
    certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --no-eff-email --redirect --non-interactive || \
        echo "WARNING: HTTPS failed. Check the domain's DNS points at this server and port 443 is open, then run:
  sudo certbot --nginx -d $DOMAIN -m $EMAIL --agree-tos --redirect"
fi

# --------------------------------------------------------------------------- check
sleep 2
HEALTH="$(curl -fsS http://127.0.0.1:8000/api/health || true)"
say "Done"
echo "API health: ${HEALTH:-NOT ANSWERING (see: journalctl -u ultrahealth-api -n 50)}"
if [ -n "$DOMAIN" ]; then
    echo "Address:    https://$DOMAIN/"
else
    echo "Address:    http://<this instance's static IP>/  (no domain yet: plain HTTP, for trying out only)"
fi
if [ -z "$(ls -A "$BASE/backups")" ]; then
    echo "Backups:    none yet; the first runs at 02:30 (or now: sudo systemctl start ultrahealth-backup)"
fi
echo
if [ "$DEMO" = 1 ]; then
    echo "DEMO sign-ins (password: test): admin, manager, manager2, store, staff"
    echo "Anyone with the address can use them. Never enter real clinic data in this demo."
else
    echo "First install? Create the Administrator account:"
    echo "  sudo $BASE/manage.sh create-user <username> <you@clinic.com> \"Your Name\" --access ADMIN"
fi
