# Ultrahealth Inventory: deploying to AWS Lightsail

This file ships inside every release package. Follow it top to bottom.

**Before you start** you need:
- an **AWS account** with access to Lightsail
- the release zip, e.g. `ultrahealth-inventory-1.0.8.zip`
- a domain name you can add DNS records to (optional to try it out, needed for HTTPS)
- about 30 minutes

Everything runs on **one Lightsail instance** (a small Ubuntu server):

| Part | Where it lives on the server | What it is |
|---|---|---|
| Website (`public_html/` in the package) | `/var/www/ultrahealth` | the built React site, served by **nginx** |
| Backend (`inventory_api/`) | `/srv/ultrahealth/inventory_api` | the Python app, run by **Gunicorn** under systemd as the `inventory` account |
| Database | MySQL 8 on the same server | listens on the server itself only, never the internet |
| Settings | `/etc/ultrahealth/inventory.env` | `SECRET_KEY` and the database password, readable by root and the app only |
| Backups | `/srv/ultrahealth/backups` | daily, compressed, newest 14 kept |

### What the world can see, and what it cannot

**Only `/var/www/ultrahealth` is published.** Every file in it can be fetched by anyone who types its address. The backend, the settings file and the backups are elsewhere on the server, and nginx never serves them. So: **never copy anything into `/var/www/ultrahealth` that is not part of the website** — not a database dump, not a spreadsheet, not this zip.

Three things guard this for you:
1. The release package contains **only** the built website in `public_html/`.
2. nginx refuses `.md .rtf .sql .log .db .sqlite3 .env .ini .cnf .bak .zip .gz .py`, `guide.html`, `schema.html` and any dot-file, and folder listings are off.
3. The Lightsail firewall opens only SSH, HTTP and HTTPS. MySQL accepts connections from the server itself only.

---

## Demo on a free / 512 MB instance

To show the app before the real setup: no MySQL, the data is one SQLite file, and it comes loaded with sample products and the test sign-ins `admin`, `manager`, `manager2`, `store`, `staff` (password `test`).

```bash
git clone <this repo> && cd <the folder holding deploy/ and inventory_api/>
sudo bash deploy/lightsail/install.sh --demo
```

Open `http://<static-ip>/`. **Anyone with the address can sign in**, so never enter real clinic data. Update it with `git pull` and the same command. The demo can't be turned into the real system in place: install the real one (section A) on a new instance.

---

## A. First deployment

### 1. Create the server (Lightsail console → Create instance)
| Field | Value |
|---|---|
| Region | **Mumbai (ap-south-1)**, closest to the clinic |
| Platform / blueprint | **Linux/Unix → OS Only → Ubuntu 24.04 LTS** |
| Plan | **2 GB memory** or larger (MySQL and the app share it; 1 GB works but is tight) |
| Name | `ultrahealth-inventory` |

Then, on the instance page:
1. **Networking → Attach static IP** → create one. Without it the address changes when the server restarts.
2. **Networking → IPv4 firewall → Add rule → HTTPS (443)**. SSH (22) and HTTP (80) are already there. Optionally restrict SSH to your own IP.
3. **Snapshots → Enable automatic snapshots** (a daily copy of the whole server, kept 7 days).
4. At your domain's DNS provider, add an **A record** for the domain (e.g. `inventory.ultrahealth.in`) pointing at the static IP. It can take a few minutes to take effect.

### 2. Upload the package (on your Mac)
Download the instance's SSH key: Lightsail → **Account → SSH keys → Download** (e.g. `LightsailDefaultKey-ap-south-1.pem`).

```bash
chmod 600 ~/Downloads/LightsailDefaultKey-ap-south-1.pem
scp -i ~/Downloads/LightsailDefaultKey-ap-south-1.pem ultrahealth-inventory-1.0.8.zip ubuntu@<static-ip>:~
ssh -i ~/Downloads/LightsailDefaultKey-ap-south-1.pem ubuntu@<static-ip>
```

### 3. Install (on the server)
```bash
sudo apt-get update && sudo apt-get install -y unzip
unzip ultrahealth-inventory-1.0.8.zip -d ultrahealth-inventory-1.0.8
cd ultrahealth-inventory-1.0.8
sudo bash deploy/lightsail/install.sh inventory.ultrahealth.in you@clinic.com
```

Leave out the domain and email to try it on the bare IP first (plain HTTP; don't let anyone sign in for real that way).

`install.sh` installs nginx, MySQL 8 and Python; creates the database, its account, a random `SECRET_KEY` and the settings file; runs **`manage.py deploy`** (tables, the protective triggers, the four access levels with their permissions, the starter categories and units, then the **pre-flight check**); starts the app, nginx and the daily backup; and, with a domain and email, gets a free **HTTPS certificate** from Let's Encrypt that renews itself.

### 4. Create the Administrator
```bash
sudo /srv/ultrahealth/manage.sh create-user <your-username> <you@clinic.com> "Your Name" --access ADMIN
```
It asks for your password (8+ characters). `/srv/ultrahealth/manage.sh` runs any `manage.py` command as the app, with its settings.

### 5. Check it's live
- `https://<your-domain>/api/health` shows `{"status":"ok","version":"1.0.8"}`
- `https://<your-domain>/` shows the sign-in page. Sign in with the admin account.

### 6. Load the real data (in the app)
1. **Administrator → Lists → Categories**: add any categories beyond the three that come with the system. Each one says whether its stock expires and whether its bills need an HSN code.
2. **Administrator → Users → Import users**: fill in `users_template.csv` (downloadable from that panel). The `access` column says what each person may do and where: e.g. `MAKER: Pantry; MAKER: Housekeeping`, `CHECKER: Clinic Consumables`, or simply `STORE` / `ADMIN`. Click **Check file**, then **Import**. The screen shows each person's **temporary password once**. Download them and hand them out. Everyone must choose their own password at first sign-in. Include **at least two checkers in every category**.
3. **Administrator → Lists → The clinic**: enter the clinic's **GSTIN**. It decides whether bills are CGST + SGST (vendor in the same state) or IGST. (Or: `sudo /srv/ultrahealth/manage.sh set-clinic --gstin <GSTIN>`.)
4. **A Store or Checker account**, in this order, because products point to vendors and HSN codes:
   1. **Vendors → Import vendors** (`vendors_template.csv`). Every GSTIN is checked, including its check digit.
   2. **Lists → HSN codes → Import** (`hsn_template.csv`), with each code's GST rate.
   3. **Inventory → Import products** (`products_template.csv`) with the current stock count. A product's `unit` and `hsn` must match the lists; several rows with the same `sku` become separate batches (`batch_no`, `expiry_date`). A product carries no vendor — the vendor is recorded on each purchase bill.

   From then on, clinic stock comes in by entering the vendor's bill in **Purchase bills** and posting it.
5. Access can be changed later on the Users screen (**Edit** under "Can do"), one tick per level per category.

### 7. Final check
```bash
sudo /srv/ultrahealth/manage.sh preflight
```
Every line should say `PASS`. It FAILs if a test password such as `test` exists, the secret key is missing, tables are out of date, or the data doesn't add up. It WARNs if there are fewer than two checkers.

Backups run every day at 02:30 (server time, UTC). To run one now and see it:
```bash
sudo systemctl start ultrahealth-backup && sudo ls -l /srv/ultrahealth/backups
```

---

## B. Moving from GoDaddy (only if the clinic is already using the GoDaddy site)

Do this after section A steps 1–3, **instead of** steps 4 and 6: the data, accounts and passwords all come across.

1. **On GoDaddy** (cPanel → Terminal, inside the Python app's virtual environment): stop people using the old site, then
   ```bash
   cd ~/inventory_api && python manage.py backup
   ls -t ~/inventory_backups/ | head -1        # the file just written
   ```
   Download that `inventory-….sql.gz` with cPanel → File Manager.
2. **Copy it to the server** (on your Mac):
   ```bash
   scp -i <key>.pem inventory-YYYYMMDD-HHMMSS-NNNNNN.sql.gz ubuntu@<static-ip>:~
   ```
3. **Load it** (on the server) — this replaces the empty database the install made:
   ```bash
   sudo systemctl stop ultrahealth-api
   gunzip -c ~/inventory-YYYYMMDD-HHMMSS-NNNNNN.sql.gz | sudo mysql inventory
   cd ~/ultrahealth-inventory-1.0.8 && sudo bash deploy/lightsail/install.sh <domain> <email>
   ```
   Running `install.sh` again brings the restored database up to this version and restarts the app. If GoDaddy's database was MariaDB and the load stops on an unknown collation, replace `utf8mb4_uca1400_ai_ci` with `utf8mb4_0900_ai_ci` in the file and load it again.
4. `sudo /srv/ultrahealth/manage.sh preflight` must say **READY**. Sign in with an existing account.
5. Point the domain's **A record** at the Lightsail static IP (section A step 1.4). Once the new site has run for a week, delete the downloaded backup files (`rm ~/inventory-*.sql.gz`) and cancel the GoDaddy hosting plan.

---

## C. Updating to a new version

1. Upload and unzip the new package on the server (section A steps 2–3), then run the same command:
   ```bash
   cd ~/ultrahealth-inventory-<new-version>
   sudo bash deploy/lightsail/install.sh <domain> <email>
   ```
   It keeps the running version as `/srv/ultrahealth/inventory_api_previous` and `/srv/ultrahealth/site_previous` (your rollback), **backs up the database**, applies only the new database changes, keeps your users, requests, stock and settings, re-checks the access levels, runs the pre-flight check and restarts the app.
2. Open `/api/health` and check the new version number.

## D. If something goes wrong (rollback)
1. Put the previous version back and restart:
   ```bash
   sudo rsync -a --delete /srv/ultrahealth/inventory_api_previous/ /srv/ultrahealth/inventory_api/
   sudo rsync -a --delete --exclude .well-known /srv/ultrahealth/site_previous/ /var/www/ultrahealth/
   sudo systemctl restart ultrahealth-api
   ```
2. If the database was changed by the failed update, restore the backup made just before it:
   ```bash
   sudo ls -t /srv/ultrahealth/backups/            # newest first
   sudo systemctl stop ultrahealth-api
   sudo gunzip -c /srv/ultrahealth/backups/inventory-YYYYMMDD-HHMMSS-NNNNNN.sql.gz | sudo mysql inventory
   sudo systemctl start ultrahealth-api
   ```
   This **replaces** the database with the backup, so anything entered after the backup is lost.
3. If the whole server is lost: Lightsail → **Snapshots** → create a new instance from the latest automatic snapshot, then move the **static IP** to it.

## E. Useful commands (on the server)
| Command | Does |
|---|---|
| `sudo /srv/ultrahealth/manage.sh preflight` | Is everything ready and healthy? |
| `sudo /srv/ultrahealth/manage.sh check` | Data consistency only |
| `sudo systemctl start ultrahealth-backup` | Backup now |
| `sudo /srv/ultrahealth/manage.sh set-clinic --gstin <GSTIN> [--name ...]` | Set the clinic's GSTIN (decides CGST+SGST vs IGST) |
| `sudo systemctl status ultrahealth-api` | Is the app running? |
| `sudo journalctl -u ultrahealth-api -n 100` | The app's recent log (errors, requests) |
| `sudo systemctl restart ultrahealth-api` | Restart the app |
| `sudo tail -n 50 /var/log/nginx/error.log` | Web server errors |
| `sudo certbot renew --dry-run` | Check HTTPS renewal works (it renews by itself) |
