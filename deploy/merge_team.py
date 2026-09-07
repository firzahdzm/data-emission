"""Replace only `team:` in the live config; leave every other key alone.

The roster is the only thing a deploy changes. admin_users, polling and
database.path are VPS-local and must survive — copying the whole file
would silently overwrite them with whatever the laptop happens to hold.
"""
import shutil
import sys
import time

import yaml

live_path, new_path = sys.argv[1], sys.argv[2]

with open(live_path) as f:
    live = yaml.safe_load(f)
with open(new_path) as f:
    incoming = yaml.safe_load(f)

if "team" not in incoming:
    sys.exit("incoming config has no `team:` — refusing to touch the live one")

backup = f"{live_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(live_path, backup)

before = sum(len(p["hotkeys"]) for p in live.get("team", []))
live["team"] = incoming["team"]
after = sum(len(p["hotkeys"]) for p in live["team"])

with open(live_path, "w") as f:
    yaml.safe_dump(live, f, sort_keys=False, allow_unicode=True)

print(f"backup   : {backup}")
print(f"team     : {before} -> {after} wallets, {len(live['team'])} people")
print(f"admin    : {live.get('admin_users')}  (kept)")
print(f"polling  : {live.get('polling')}  (kept)")
print(f"database : {live.get('database')}  (kept)")
