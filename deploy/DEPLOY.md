# Deploy to a Linux VPS

Target: Ubuntu 22.04+ / Debian 12+ (anything with systemd + python3.11+).

## 1. Provision the server

Minimum spec (more than enough):
- 1 vCPU
- 1 GB RAM (2 GB nyaman)
- 10 GB SSD
- Outbound HTTPS to `api.taostats.io`

## 2. System prerequisites

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx apache2-utils

# (Optional) Python 3.12+ if your distro ships an older default:
# sudo apt install software-properties-common
# sudo add-apt-repository ppa:deadsnakes/ppa
# sudo apt install python3.12 python3.12-venv
```

## 3. Create service user + directory

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/emission-tracker emission
sudo mkdir -p /opt/emission-tracker /opt/emission-tracker/data /opt/emission-tracker/logs
sudo chown -R emission:emission /opt/emission-tracker
```

## 4. Clone + install

```bash
sudo -u emission -H bash <<'EOF'
cd /opt/emission-tracker
git clone https://github.com/firzahdzm/data-emission.git .
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
EOF
```

## 5. Configure `.env`

```bash
sudo -u emission tee /opt/emission-tracker/.env > /dev/null <<EOF
TAOSTATS_API_KEY=tao-YOUR-REAL-KEY-HERE
LOG_LEVEL=INFO
EOF
sudo chmod 600 /opt/emission-tracker/.env
sudo chown emission:emission /opt/emission-tracker/.env
```

Create the runtime config from the example template:

```bash
sudo -u emission cp /opt/emission-tracker/config.example.yaml /opt/emission-tracker/config.yaml
sudo -u emission nano /opt/emission-tracker/config.yaml   # add the real team roster
sudo chmod 640 /opt/emission-tracker/config.yaml
```

`config.yaml` is gitignored; team rosters never enter the public repo. Transfer your real roster from a trusted source (your laptop, password manager) — not from a public channel.

## 6. Install systemd unit

```bash
sudo cp /opt/emission-tracker/deploy/emission-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now emission-tracker
sudo systemctl status emission-tracker
```

Watch logs:

```bash
sudo journalctl -u emission-tracker -f
```

Sanity check (local on the VPS):

```bash
curl http://127.0.0.1:8000/api/healthz   # → "ok"
```

First real snapshot completes in ~5.5 minutes (22 hotkeys × 15s rate-limit gap). Check progress:

```bash
sudo journalctl -u emission-tracker --since "10 min ago" | grep snapshot
```

## 7. Expose via nginx + Basic Auth + HTTPS

The app has no built-in auth — **do not bind it to 0.0.0.0 directly**.

```bash
# Basic auth file
sudo htpasswd -c /etc/nginx/.htpasswd_emission YOUR_USERNAME
# (you'll be prompted for a password)

# nginx config
sudo cp /opt/emission-tracker/deploy/nginx.conf.example /etc/nginx/sites-available/emission-tracker
# Edit server_name + cert paths to match your domain:
sudo nano /etc/nginx/sites-available/emission-tracker
sudo ln -s /etc/nginx/sites-available/emission-tracker /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Let's Encrypt (replaces the listen 80 block with HTTPS redirect)
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d emission.example.com
```

Open https://emission.example.com in a browser and log in.

## 8. Update workflow

When you change the code on your laptop and push:

```bash
# on VPS
cd /opt/emission-tracker
sudo -u emission git pull
sudo -u emission .venv/bin/pip install -e .     # only if pyproject deps changed
sudo systemctl restart emission-tracker
sudo journalctl -u emission-tracker -f
```

The startup cleanup will mark any snapshots stuck in `in_progress` (from the restart) as `failed`, so the `/history` view stays accurate.

## 9. Backups

The whole state lives in one file: `/opt/emission-tracker/data/emissions.db`. A nightly cron is sufficient:

```bash
sudo crontab -e
# add:
0 3 * * * sqlite3 /opt/emission-tracker/data/emissions.db ".backup '/opt/emission-tracker/data/emissions-$(date +\%F).db'" && find /opt/emission-tracker/data -name 'emissions-*.db' -mtime +14 -delete
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Service won't start, `ImportError` | `pip install -e .` skipped | Re-run step 4 install |
| 401 on browser | Basic auth file missing/wrong | Re-run `htpasswd` (step 7) |
| All snapshots `failed` | Wrong `TAOSTATS_API_KEY` or no internet | Check `.env`, `curl api.taostats.io` from VPS |
| `Permission denied: data/emissions.db` | Wrong ownership | `chown -R emission:emission /opt/emission-tracker/data` |
| Dashboard shows nothing for 5+ minutes | First snapshot in progress | Wait ~6 min, check `journalctl` |
