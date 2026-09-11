# Signed on-chain actions from the dashboard

Two admin buttons per coldkey card: pay tournament fees, and unstake
everything on subnet 56. Both sign real transactions with real coldkeys.

## Why this needs a second service

The tracker runs as `emission` (uid 999) with `ProtectHome=true` and
`ProtectSystem=strict`. The wallets live in `/root/.bittensor/wallets`,
owned by root, and all fifteen coldkeys are passphrase-encrypted
(`$NACL` header). The service cannot see them, by design.

Handing the web app the wallets and their passphrases would make the
dashboard the key to every wallet. It is reachable over the network, it
renders user-supplied data, and its admin check trusts an
`X-Remote-User` header (see Prerequisite below). That is the wrong place
to keep a treasury.

Instead a second unit, `emission-signer`, holds the wallet access and
exposes exactly two operations over a unix socket. It is the only
component that can sign, and it can only sign the two shapes below.

## The wallet unlock value is typed, not stored

The admin types the wallet's unlock value into the dashboard when they
click, and it travels with that one request. The signer puts it in the
environment of a single btcli subprocess and keeps nothing.

Nothing here is ever scheduled or retried, so unattended signing was never
a requirement — which is the only thing storing secrets on disk would have
bought. Skipping it changes the worst case materially: with values on disk,
one host compromise yields the encrypted keyfiles *and* everything needed
to open all fifteen, at once. Without, an attacker holds keyfiles and must
wait to capture each value as it is used; wallets never touched never leak.

The cost, stated plainly: the value now passes through the web tier, the
least-trusted component in this design. It is bounded because the dashboard
holds no keyfiles — a captured value alone moves nothing — but it is a real
exposure, and it is why HTTPS is mandatory rather than advisable.

This is a deliberate exception to "the caller names nothing consequential"
below, and it is a different kind of field: it does not choose *what*
happens — not the destination, not the amount, not the subnet — it only
proves the person asking is allowed to ask. The request dataclass declares
it `repr=False` and the API model types it `SecretStr`, so an accidental
log line or traceback prints a mask rather than the value.

## Security properties

What the split buys, stated precisely:

- **The destination address is compiled into the signer, not passed to
  it.** A caller asks to "pay the text tournament for coldkey X"; it
  cannot say where the money goes. An attacker holding the web app can
  send tournament fees to the tournament, and nothing else.
- **Unstake moves nothing off the coldkey.** Alpha becomes free TAO in
  the same wallet. It is destructive (slippage, lost position) but not a
  theft route, which is why it fits the same envelope.
- **Per-request and per-day caps live in the signer**, so a bug or a
  stuck retry loop in the web app cannot spend past them.
- **Every attempt is logged by the signer** before it is attempted, so
  the audit trail survives the web app being wrong or compromised.

What it does not buy: anyone with root on the VPS still has everything.
This bounds damage from a web-app compromise, not from host compromise.

## Prerequisite: the X-Remote-User hole

`auth.py` trusts the `X-Remote-User` header verbatim. nginx overwrites it
from Basic Auth, but uvicorn listens on `127.0.0.1:8000`, so anything on
the host that can reach that port can set the header and be an admin.
Read-only, that is a moderate risk. With these buttons it is a path to
funds.

Fix before shipping either button: nginx adds a shared secret header
(`X-Auth-Proxy: <random>`, from the same file the app reads), and the app
rejects any request whose secret does not match, ignoring `X-Remote-User`
entirely on those requests. Cheap, and it closes the gap.

## The signer

A systemd unit running as a dedicated `signer` user that can read the
wallet directory. It stores no unlock values of its own — each request
carries the one it needs, per the section above.

Socket: `/run/emission-signer/emission-signer.sock`, mode 0660, in a
`RuntimeDirectory=` group-owned by `emission`, so only the tracker can
reach it.

Two requests, JSON lines:

```
{"op": "pay_tournament", "coldkey": "5Fnh…", "types": ["text", "env"]}
{"op": "unstake_all",    "coldkey": "5Fnh…"}
```

The signer resolves `coldkey` to a btcli wallet name from
`btcli wallet list --json-output` — the mapping is derivable, so no
hand-maintained table can drift out of date. An unknown coldkey is
refused.

It computes the amount itself from its own copy of the fee table. The
caller names tournament types; it never names a number.

### Commands issued

```bash
btcli wallet transfer \
    --destination <hard-coded tournament address> \
    --amount <computed> \
    --wallet-name <resolved> \
    --no-prompt --json-output

btcli stake remove \
    --unstake-all --netuid 56 --all-hotkeys \
    --safe-staking --tolerance 0.05 --allow-partial-stake \
    --wallet-name <resolved> \
    --no-prompt --json-output
```

`--safe-staking --tolerance 0.05` is not optional: the shared wallet
holds ~526 alpha, and dumping that into the pool moves the price against
itself. With partial unstake allowed, a run that would exceed 5% slippage
completes as much as it can rather than either failing outright or
selling into a hole.

`--unstake-all`, not `--all-alpha`: the latter restakes to Root instead
of leaving free TAO, which defeats the purpose (funding fees).

**To verify before enabling the button:** that `--unstake-all` combined
with `--netuid 56` really is subnet-scoped, and does not reach stake on
other subnets. Test with one small wallet and read the result before
wiring the UI.

## Tracker side

`tournament.fees_tao` and the destination live in `config.yaml` too, for
display and the balance check. The signer keeps its own copy and does not
trust this one.

New table `signed_actions`: id, coldkey, op, types, amount_rao,
status (`pending`/`ok`/`failed`), tx_hash, error, requested_by,
requested_at, finished_at. Written `pending` before the socket call, so a
crash mid-flight leaves evidence rather than silence.

Endpoints, both admin-only:

- `POST /api/tournament/pay/{coldkey}` body `{"types": [...]}`
- `POST /api/stake/unstake-all/{coldkey}`

Both refuse when a `pending` row exists for that coldkey — a double click
or a page reload must not pay twice.

## UI

Inside each coldkey card:

- three checkboxes (text 0.7 τ / image 0.4 τ / env 0.6 τ) with a running
  total
- **Pay fee** — disabled unless a type is checked and the wallet's free
  balance covers the total. Ten of fifteen wallets currently cannot
  afford the cheapest tournament, so this is the common state, not the
  exception.
- **Unstake all** — behind a typed confirmation (the coldkey's label),
  because it is irreversible and costs slippage.

Both buttons follow the existing refresh convention: every card's
controls grey out while any signed action is in flight, and the card
refreshes when it finishes.

## Never automatic

Neither operation is ever scheduled, retried on its own, or triggered by
startup, the daily balance job, or any other timer. Both fire only from
an admin's click. A failed action stays failed and visible until someone
presses the button again — silent retries of a money-moving call are how
one intended payment becomes three.

## Out of scope

Sending to any address other than the tournament wallet. Staking. Any
operation that can move funds to a caller-chosen destination. Adding one
later means changing the signer, deliberately — which is the point.
