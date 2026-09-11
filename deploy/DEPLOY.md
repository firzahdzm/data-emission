# Deploy to a Linux VPS

Target: Ubuntu 22.04+ / Debian 12+ (anything with systemd + python3.11+).

## 1. Provision the server

Minimum spec (more than enough):
- 1 vCPU
- 1 GB RAM (2 GB nyaman)
- 10 GB SSD
- Outbound HTTPS to `api.taostats.io` and `api.gradients.io`

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

Each entry under a person's `hotkeys` is one wallet. The mapping form
carries the owning coldkey and a label; the bare-string form still works
for a hotkey whose coldkey is unknown:

```yaml
team:
  - name: Firza
    hotkeys:
      - hotkey: 5DXVNvDm...
        coldkey: 5Fnhiibt...
        label: I
      - hotkey: 5GpcTKW7...
        coldkey: 5HERhLCK...
        label: (old)
```

All of a person's wallets accumulate into the same per-period total, so a
pre-rotation hotkey that is still earning keeps counting alongside the new
ones. Coldkeys drive the wallet-balance cards on the dashboard.

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

## 7a. Grant admin powers (settle/unsettle periods)

The web UI has a `[Close period]` button and a `[Delete settlement]` button that only appear for usernames listed in `config.yaml` under `admin_users`. nginx forwards the Basic Auth username to the app via the `X-Remote-User` header (this is wired up in the example nginx config).

To make a Basic-Auth user an admin, edit `config.yaml`:

```yaml
admin_users:
  - firza     # must exactly match the htpasswd username used at /etc/nginx/.htpasswd_emission
```

```bash
sudo systemctl restart emission-tracker
```

Non-admin users see no Close/Delete buttons; even hitting `POST /api/settlements` directly returns 403.

### Locking the admin header to nginx

`X-Remote-User` is only meaningful if nginx is the only party that can set
it. Generate a secret, put the same value in both places, and the app will
ignore the header on any request that arrives without it:

```bash
openssl rand -hex 32
# → paste into proxy_set_header X-Auth-Proxy in the nginx site
# → and into proxy_secret in /opt/emission-tracker/config.yaml
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart emission-tracker
```

Verify from the VPS that bypassing nginx no longer works:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
     -H "X-Remote-User: admin" http://127.0.0.1:8000/api/settlements/999999
# 401 = closed. 404 = the header still works directly; the secret is not matching.
```

`EMISSION_DEV_USER` must never appear in `/opt/emission-tracker/.env`. It is
a local-development hatch that makes every request an admin. The app now
ignores it whenever `proxy_secret` is set (and logs a warning when it does),
but on a box where the secret is still empty it is a complete bypass of the
admin gate — and the admin gate is what stands between a stray HTTP request
and the wallets.

## 7b. The signer service

The two money buttons (pay tournament fees, unstake all) are not signed by
the dashboard. A separate unit, `emission-signer`, runs as its own user,
holds the coldkey passphrases, and accepts exactly two operations over a
unix socket with the destination address and fee table hard-coded. A
compromised dashboard can therefore pay the tournament address and nothing
else. Everything below is what makes that split actually work on the host.

Run these in order.

### 1. Create the signer user

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin signer
# The socket is 0660 signer:emission — the tracker reaches it through the
# group, so the signer must be in it.
sudo usermod -aG emission signer
```

### 2. Make the wallets readable by the signer

`ReadOnlyPaths=/root/.bittensor/wallets` in the unit only *restricts* — it
grants nothing. `/root` is mode 0700, so the `signer` user cannot traverse
into it and `btcli wallet list` fails on every request. Pick one:

**Option A (recommended) — move the wallets out of `/root`.** Cleaner,
because nothing outside root ever gains a foothold in root's home:

```bash
sudo mkdir -p /var/lib/emission-signer/wallets
sudo cp -a /root/.bittensor/wallets/. /var/lib/emission-signer/wallets/
sudo chown -R signer:signer /var/lib/emission-signer/wallets
sudo chmod -R go-rwx /var/lib/emission-signer/wallets
sudo chmod 0700 /var/lib/emission-signer/wallets
```

Then set `wallet_path: /var/lib/emission-signer/wallets` in the signer
config below, and change `ReadOnlyPaths=` in the unit to match. Verify the
copy before deleting the originals — keep an offline backup of the coldkeys
regardless.

**Option B — an ACL, leaving the wallets where they are:**

```bash
sudo apt install acl   # if getfacl/setfacl are missing
sudo setfacl -m u:signer:x /root
sudo setfacl -R -m u:signer:rX /root/.bittensor/wallets
sudo -u signer test -r /root/.bittensor/wallets && echo "signer can read the wallets"
```

Be clear-eyed about what Option B does: granting a service user traversal
on `/root` is a real widening of access. It is not "just an x bit" — any
path under `/root` whose own mode permits reading becomes reachable by the
`signer` user from that moment on. Option A avoids the question entirely.

### 3. Install the signer config

```bash
sudo mkdir -p /etc/emission-signer
sudo cp /opt/emission-tracker/deploy/signer.example.yaml \
        /etc/emission-signer/config.yaml
sudo chown root:root /etc/emission-signer/config.yaml
sudo chmod 0644 /etc/emission-signer/config.yaml
# Check wallet_path matches the choice made in step 2, and that
# max_transfer_tao / daily_cap_tao are the ceilings you want. They are
# enforced here, where a compromised dashboard cannot reach them.
sudo nano /etc/emission-signer/config.yaml
```

### 4. Write the passphrase files

**These files contain the plaintext passphrases to your coldkeys.** Anyone
who reads one plus the matching wallet file can move every TAO in that
coldkey. They must be `0600 root:root` — the signer never reads them
directly; systemd reads them as root and hands each one to the unit as a
credential file under `$CREDENTIALS_DIRECTORY`, which is why they do not
need to be readable by the `signer` user and must not be.

```bash
sudo install -d -m 0700 -o root -g root /etc/emission-signer/passphrases
sudo install -m 0600 -o root -g root /dev/null /etc/emission-signer/passphrases/goy
sudo nano /etc/emission-signer/passphrases/goy   # the passphrase, nothing else
# repeat for every wallet, then confirm:
sudo ls -l /etc/emission-signer/passphrases/     # every line must read -rw------- root root
```

Do not add these to backups that leave the host, and do not paste them into
a shell where they land in `~/.bash_history`.

### 5. List every wallet in the unit

`LoadCredential=` has one line per wallet, and the name after `wallet-`
must match the btcli wallet name exactly — the signer looks up
`wallet-<name>` when it signs.

```bash
sudo -u signer btcli wallet list --wallet-path /root/.bittensor/wallets \
     --no-prompt --json-output | python3 -c \
  'import json,sys; [print(w["name"]) for w in json.load(sys.stdin)["wallets"]]'
```

Put one `LoadCredential=wallet-<name>:/etc/emission-signer/passphrases/<name>`
line per printed name into `deploy/emission-signer.service`, replacing the
three placeholder lines. A wallet with no line will fail at sign time with a
missing credential file, not at start time.

### 6. Install and start the unit

```bash
sudo cp /opt/emission-tracker/deploy/emission-signer.service \
        /etc/systemd/system/emission-signer.service
sudo systemctl daemon-reload
sudo systemctl enable --now emission-signer
sudo systemctl status emission-signer --no-pager
```

`RuntimeDirectory=emission-signer` makes systemd create and own
`/run/emission-signer` (0750 `signer:emission`) for the lifetime of the
unit, and remove it on stop. The socket lives inside it.

### 7. Verify the passphrase environment variable

**Do this before trusting either button.** The signer no longer passes the
passphrase under a fixed name — the correct env var name is *derived from
the coldkey keyfile path* (`<wallet_path>/<wallet_name>/coldkey`,
uppercased, with every `/` and `.` turned into `_`, prefixed `BT_PW_`), by
`coldkey_password_env_var()` in `src/emission_tracker/signer/btcli.py`.
This mirrors `bittensor_wallet`'s own derivation
(`Wallet(...).coldkey_file.env_var_name()`), which is why relocating the
wallets (Option A above) changes the variable name too — but it is still
worth re-checking the two agree, since a future bittensor release could
change its derivation rule. First, ask the installed bittensor for the
authoritative name and compare it to what our function computes for the
same `wallet_path`/name:

```bash
sudo -u signer /root/.venv/bin/python3 -c "
from bittensor_wallet import Wallet
print(Wallet(name='goy', path='<your wallet_path>').coldkey_file.env_var_name())
"
sudo -u signer /opt/emission-tracker/.venv/bin/python3 -c "
from emission_tracker.signer.server import coldkey_password_env_var
print(coldkey_password_env_var('<your wallet_path>', 'goy'))
"
```

**The two commands must print the same string.** If they differ, the
installed bittensor changed its derivation and `coldkey_password_env_var`
in `src/emission_tracker/signer/btcli.py` must be updated to match before
any button will work.

Then unlock one coldkey locally, exporting the *computed* variable name
(not `BT_WALLET_PASSWORD`) — no chain interaction, no funds move:

```bash
sudo -u signer env "$(sudo -u signer /opt/emission-tracker/.venv/bin/python3 -c "
from emission_tracker.signer.server import coldkey_password_env_var
print(coldkey_password_env_var('/root/.bittensor/wallets', 'goy'))
")"='<that wallet's passphrase>' \
  /opt/emission-tracker/.venv/bin/python -c "
from bittensor_wallet import Wallet
w = Wallet(name='goy', path='/root/.bittensor/wallets')
w.unlock_coldkey()
print('passphrase accepted')
"
```

If it prints `passphrase accepted`, the name is right. **If it prompts
you for a password instead, the variable name is wrong for this
bittensor version** — the mismatch should already have shown up in the
comparison step above; fix `coldkey_password_env_var` in
`src/emission_tracker/signer/btcli.py` before the buttons will work at
all.

### 8. Restart the tracker and check it can reach the socket

The tracker unit now lists `/run/emission-signer` in `ReadWritePaths=`
(under `ProtectSystem=strict` everything outside `/dev`, `/proc` and `/sys`
is read-only, and `connect()` on a unix socket needs write permission on
the inode) and orders itself `After=emission-signer.service`. Reinstall it
and restart:

```bash
sudo cp /opt/emission-tracker/deploy/emission-tracker.service \
        /etc/systemd/system/emission-tracker.service
sudo systemctl daemon-reload
sudo systemctl restart emission-tracker
```

Then verify:

```bash
# Directory and socket, with the expected owner, group and mode:
sudo ls -ld /run/emission-signer                       # drwxr-x--- signer emission
sudo ls -l  /run/emission-signer/emission-signer.sock  # srw-rw---- signer emission

# The tracker's own user must be able to connect:
sudo -u emission python3 -c "
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('/run/emission-signer/emission-signer.sock')
print('tracker can reach the signer')
"
```

A `PermissionError` here means the `emission` user cannot traverse
`/run/emission-signer` or write the socket inode — check the group on both,
and that the tracker unit really was reinstalled. `FileNotFoundError` means
the signer is not running; read `journalctl -u emission-signer`.

Refused attempts (unknown coldkey, per-request cap, daily cap) are logged by
the signer, not the dashboard. That log is the record that survives the web
app being compromised:

```bash
sudo journalctl -u emission-signer -n 50 --no-pager
```

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

Schema changes apply themselves: `init_schema` runs idempotent `ALTER TABLE`
statements at every startup, so no manual migration step exists.

**When the roster format changes, the code pull is not enough.** `config.yaml`
is gitignored, so `git pull` cannot deliver a new roster. Copy it from your
laptop first, then restart:

```bash
# on your laptop
scp config.yaml YOUR_USER@YOUR_VPS:/tmp/config.yaml

# on the VPS
sudo install -o emission -g emission -m 640 /tmp/config.yaml /opt/emission-tracker/config.yaml
rm /tmp/config.yaml
sudo systemctl restart emission-tracker
```

Skipping this leaves the tracker on the old roster: the new hotkeys are never
polled and the coldkey cards stay empty.

## 8a. Wallet and tournament balances

The dashboard's coldkey cards come from a second, slower job:

- **TaoStats** `account/latest/v1` — TAO balance per coldkey
- **Gradients** `api.gradients.io/tournament/balance/{coldkey}` — tournament
  deposit; no API key, and a 404 simply means that coldkey never paid a buy-in

It runs every `polling.balance_interval_hours` (default 24) and once at
startup when `run_on_startup` is true — without that startup run the cards
would sit empty until the next day, since an interval job's first firing is a
full interval away. One run is about four minutes for 15 coldkeys.

Admins can also trigger it from the dashboard button. Every TaoStats caller
shares one rate limiter, so the manual run, the daily job and the emission
snapshot cannot together exceed 5 requests/minute; a second click during a run
is refused with 409 rather than queued.

Check it after a deploy:

```bash
sudo journalctl -u emission-tracker | grep "balance refresh"
# → balance refresh — 15 coldkeys, wallet 15 ok / 0 fail, tournament 1 ok / 14 none / 0 fail
```

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
| Dashboard shows nothing for 5+ minutes | First snapshot in progress | Wait ~10 min, check `journalctl` |
| Coldkey cards empty | `config.yaml` on the VPS has no coldkeys, or the balance job hasn't run | Copy the roster over (step 8), restart, watch for "balance refresh" in the log |
| All tournament balances blank | Egress to `api.gradients.io` blocked | `curl https://api.gradients.io/tournament/balance/<coldkey>` from the VPS |
| Snapshot slower than usual right after a restart | Snapshot and balance seeds share the rate limiter at startup | Expected; ~10 min instead of ~9 |
