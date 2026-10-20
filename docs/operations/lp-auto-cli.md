# Stable macOS LP Auto command

`lpauto` is a user-local executable for the **current managed macOS Prediction
service**. It selects that service's immutable release and exact Python
interpreter on each invocation. A new terminal does not need a shell function
or a development checkout. Linux services and remote control are outside this
launcher’s scope.

## Explicit installation

Use a reviewed, committed launcher file and an existing Python interpreter. The
installer verifies its exact bytes against its tracked Git blob at the reported
source commit before publication. Modified, untracked or staged-but-uncommitted
installer bytes are refused, including changes hidden by index flags. Installation does
not install Python, edit PATH or shell startup files, deploy code, restart the
service, or change Auto, budgets or orders. Installation remains a separate
operator action; the repository change does not perform it.

```sh
# Set this to the absolute path of the reviewed launcher checkout.
LAUNCHER_CHECKOUT=/absolute/reviewed/open_trader
BOOTSTRAP=/Users/ray/projects/open_trader-runtimes/4e98ff71e808b2eb3dacde69275a28765078a23d/bin/python
"$BOOTSTRAP" -I -B "$LAUNCHER_CHECKOUT/scripts/lp_auto_launcher.py" init \
  --launcher-python "$BOOTSTRAP"
```

The supplied bootstrap path is pinned without resolving its venv symlink. It
runs the standalone launcher, not the application. The current service
interpreter is discovered separately. Keep the bootstrap available; if it is
removed, use an explicitly selected existing Python to run `repair`.

Defaults are `~/.local/bin/lpauto` and the three owned files `launcher.py`,
`config.json` and `receipt.json` under `~/.local/share/open-trader/lp-auto/`.
Repeat `init` is supported for this owned installation. Unrelated commands,
pre-existing payload files and symlink escapes are refused. The configuration
holds the Prediction locator and explicit tool paths, never a cached target
release or service interpreter. The receipt records the launcher source SHA
and SHA-256 separately from the current service SHA.

For an existing user-writable PATH directory, pass `--bin-dir /absolute/bin`.
You can also select `--payload-dir /absolute/payload` and
`--plist /absolute/com.open-trader.prediction-service.plist`. Keep these same
paths for repair and uninstall. Tool path options `--launchctl`, `--lsof`,
`--ps` and `--git` are explicit installation bindings; environment variables
cannot replace them. Isolated tests use these options for OS observations.
Do not point them at unrelated services or use them to bypass verification.

## New terminal and origin checks

```sh
~/.local/bin/lpauto --help
~/.local/bin/lpauto --version --json
~/.local/bin/lpauto status --json
type -a lpauto
```

The absolute command does not depend on PATH or shell startup files. Bare
`lpauto` works when the selected bin directory is already on PATH. An old alias
or function can shadow it; inspect `type -a lpauto` and remove that definition
manually if appropriate. The launcher does not edit shell files.

Top-level help is local and needs no deployed target. Version output reports
`launcher.source_sha` and `launcher.source_sha256`, then `deployment.sha`,
`root`, `code_root`, exact `interpreter`, listener `url` and fresh process
identity. These are separate identities; package version `0.1.0` is not a
deployment version. After an independently authorized Prediction upgrade,
repeat version and status. The next invocation follows the verified new release
without reinstalling the launcher. Gateway and other service metadata do not
select the target.

## LP actions

```sh
# Read-only; this does not enable Auto or change orders.
~/.local/bin/lpauto status --json

# These control examples require their own trading authorization.
~/.local/bin/lpauto config --budget 100.00 --target-buys 5 --bid-level 2
~/.local/bin/lpauto on
~/.local/bin/lpauto off
~/.local/bin/lpauto pause
```

The launcher delegates once to the current deployed public CLI, preserving LP
argument values and order, stdout, stderr and exit status. It sanitizes inherited
Python source settings, uses the verified release cwd/PYTHONPATH and verifies
the application's import origin. If `--url` is omitted, it explicitly supplies
the verified listener URL. A supplied URL must use HTTP, a loopback IP and the
same managed endpoint, with no credentials, query, fragment or extra path.
The deployed CLI continues to own config validation, cookies/CSRF,
confirmation, secret redaction and the one-write-attempt contract. See the
[LP manual SOP](polymarket-lp-manual-sop.md#22-lp-auto-cli) for control semantics.

Spell the target option in full: `--url` or `--url=<value>`. The stable launcher
rejects `--u`, `--ur` and their `=<value>` forms before delegation. A shortened
option after a verified full URL cannot select another service.

Config saves parameters without enabling Auto. `on` uses saved parameters;
`off` and `pause` retain existing orders and exit protection. Help, version,
init and status make no control POST. An UNKNOWN write result can mean the
service accepted the command but the reply was lost. Read status before any
separately authorized next action. The launcher never retries a mutation or
automatically pauses, rolls back or repairs the service.

## Verification and failure handling

Each delegated invocation holds the **existing** Prediction release-operation
lock read-only with a nonblocking shared lock through child exit. A supported
deployment needs its exclusive lock, so it can refuse while a command is
active. The launcher does not create the lock or wait for a switch to finish.
This protects the supported deployment protocol; it cannot prevent an
administrator from bypassing that protocol.

The launcher cross-checks the on-disk plist, loaded launchd service, ready
runtime record, detached clean Git release and tracked manifest, process cwd
and start, listener, production owner and `/healthz` identity/generations.
Hidden index changes and ignored source files fail verification. It rechecks
identity before delegation. It never chooses repository main, a latest
directory, another service or a cached fallback, and never repairs runtime
metadata. Health probes use no proxy and follow no redirects.

A verified same-release restart can have a new current PID/start while the
saved ready observation is historical. Version then reports
`saved_observation_current: false`. This does not repair the record or permit
a different candidate release.

Pre-delegation failures exit 2. With `--json`, they return `result: UNKNOWN`,
`state: null`, a stable reason and a non-mutating next step:

| Reason | Operator action |
| --- | --- |
| `DEPLOYMENT_IN_PROGRESS` | Finish the separately authorized deployment, then recheck version/status. |
| `DEPLOYED_TARGET_UNVERIFIABLE` | Inspect the managed deployment through its runbook. Do not use a cached source or force a control attempt. |
| `LAUNCHER_BOOTSTRAP_MISSING` | Run explicit repair with an existing Python interpreter. |
| `LAUNCHER_SOURCE_UNVERIFIABLE` | Use the tracked launcher file from the reviewed commit; do not install modified, staged or copied untracked bytes. |
| `LAUNCHER_INSTALLATION_INCOMPLETE` | Run explicit repair or uninstall from that committed source, using the same bin/payload locations. |
| `INSTALLATION_IN_PROGRESS` | Wait for the active local installation operation to finish, then explicitly retry. |

Full plist, environment, health payloads and credentials are not included in
failure output. An unverified target is UNKNOWN, not a healthy deployment.

## Repair and uninstall

```sh
"$BOOTSTRAP" -I -B "$LAUNCHER_CHECKOUT/scripts/lp_auto_launcher.py" repair \
  --launcher-python "$BOOTSTRAP"
"$BOOTSTRAP" -I -B "$LAUNCHER_CHECKOUT/scripts/lp_auto_launcher.py" uninstall
```

Repair replaces the identified owned installation from the explicitly selected
reviewed source; it does not overwrite an unrelated executable or update service
metadata. Init, repair and uninstall acquire separate nonblocking exclusive
management guards for the canonical entry and payload resources, in deterministic
order, before reading ownership. They retain both guards through validation,
publication, cleanup or uninstall. Shared entry or payload configurations therefore
cannot interleave their management writes. A busy command exits 2 with
`INSTALLATION_IN_PROGRESS` before service probes or owned artifact changes. It
does not retry automatically. The deployment release shared lock has its separate
role described above.

The stable regular guard files sit outside the bin/payload directories. Defaults
are `~/.local/.bin.lpauto-entry.lock` and
`~/.local/share/open-trader/.lp-auto.lpauto-payload.lock`. They contain only local
resource identity markers. Symlink, nonregular or unrelated guard collisions are
refused. Uninstall retains these guards; do not remove them while management can
be active, because unlinking a guard can split competing holders.

First guard creation writes and fsyncs the complete marker in a private staging
file, locks its inode, then publishes it through an atomic no-overwrite hard
link. A failed staging write exposes no partial authoritative guard. Existing
guards are never replaced or rewritten. Interruption after publication can leave
a private staging link; the complete matching marker and same owned regular inode
remain valid for later management, without deleting or adopting unknown bytes.
Extra links do not authorize guard replacement or unlinking.

Fresh payload files are staged completely before an atomic directory
publication. Published files and the entry use atomic replacement. An interrupted
repair can leave a bounded `.transaction.json` ownership journal alongside the
normal three payload files. It records prior/proposed hashes, so explicit repair
or uninstall can accept verified old/new artifacts while refusing unknown bytes
and symlink collisions. Delegation is refused while that journal remains. A
successful repair removes it and leaves the normal three-file payload. Do not
delete the journal or manually overwrite mismatched files to force recovery.

If initial publication fails before a complete payload is published, explicit
repair can initialize it normally. If the complete payload was published but the
entry was interrupted, its receipt retains ownership for repair/uninstall.
These operations never retry a control request or repair deployment metadata.
Repair still requires a verified service target; uninstall does not need a
healthy target. It removes only the owned
entry/payload/config/receipt, preserving unrelated files, runtime records,
plists, budgets, state, orders and the stable management guards. Neither operation is deployment or trading
authorization. Repository delivery follows
[agent verification](agent-verification.md); deployment follows the existing
[release runbook](../../ops/release-deployment.md).

## C1 allocation observation settings

C1 adds optional settings to the current D2 settings CAS, HTTP config/state and
public CLI. Missing fields retain `minimum`, no weights and shadow disabled;
legacy default state and estimator payloads keep their existing shape. Requests
that omit these fields preserve saved values, including dashboard draft saves.
No dashboard controls, environment variables or separate configuration files
are added.

For an already configured target of three BUYs, an explicitly authorized
observation-only configuration can save:

```sh
open-trader prediction-arb lp-auto config \
  --allocation-mode minimum --allocation-weights 0.50,0.30,0.20 \
  --allocation-shadow-enabled true
```

Weights are Decimal strings, ordered from highest rank. They must be finite,
positive, non-increasing, match `target_buy_count` and sum **exactly** to one.
They are never normalized. Whitespace, underscores and non-ASCII digits are rejected. For zero targets, use `--allocation-weights ''`.
Saving a new target count with existing weights requires a matching weight list.
`--allocation-shadow-enabled false` disables observation. Allocation-only saves
advance the settings CAS version, preserve the trading version and do not enable
or resize orders. Budget, count and price changes retain their existing pause
and current-facts gates. Pending saves retain the previous effective settings;
the CLI reports an unconfirmed save as UNKNOWN without retrying it.

**C1 cannot activate live `rank_weights`:** saving or loading that mode fails
with `rank_weights_requires_c2`. A stored invalid allocation is reported through
`settings_error`; startup and pause/resume remain available, but native plans
stay blocked until a valid repair is committed. Overwrite the bad allocation
fields with `allocation_mode=minimum`, valid weights and a boolean shadow flag,
using the current CAS version. Only the proposed allocation is validated during
a repair save; account identity and the other settings remain validated.
Weights and the shadow flag may be saved while
live mode remains `minimum`. This is C1 only: no behavior change, shadow default
off, no C2. U2 integration, live rank-by-rank sending and cooldowns are deferred.
No per-order or per-market USDC cap is introduced.

When enabled, `diagnostics.allocation_shadow` and the
`prediction_lp_allocation_shadow` log record describe a counterfactual from the
same captured facts. They confer no send authority. The target envelope is
`max(0, min(budget - inventory_cost, min(balance, allowance)))`; actual current
BUY reservations are subtracted once for available new cash and still occupy
physical slots. Known retained BUYs keep their actual quantity. Unknown or
pending-cancel incumbents block new shadow allocations. Non-funding eligibility
exclusions are recorded before ranking; unaffordable high ranks remain in rank.
Candidates with unknown eligibility or yield are excluded with `candidate_unknown`
and counted in `candidates_unknown_count`; they do not poison the whole pool.
A failed per-market source contributes one exclusion with an unknown token.
Missing account or acceptance authority, incomplete pool/source coverage, pending-cancel
incumbents and live incumbents with unknown yield produce `state: unknown`.
Unknown-yield incumbents keep their slot and reservation. A known partial pool
leaves missing ranks as cash. The acceptance-failure and manual-read paths do
not compute a shadow; a normal live selection computes it once and reuses native
minimum yields only when identity, terms, direction and evaluation time match.

Each rank reports condition/token/market identity, price level, price, target USD,
quantity rounded down to 0.01 shares, actual USD, final risk state/reasons and
estimated hourly yield at that size when eligible. Status is `WOULD_SEND`,
`RETAINED` or `NOT_SENT`. The first reward-minimum, funds, slot, opening, risk or
unknown failure blocks all later new ranks (`prefix_blocked`); cash is not
redistributed. Final yield inversion or an unproven comparison with unselected
candidates rejects all proposed new ranks (`allocation_rank_not_proven`).
Missing facts produce `state: unknown`; a computation exception produces
`state: error`, the exception type and `shadow_exception`, without exception
text or a change to trading. Successful calculations report leftover cash,
send/retained/not-sent/missing counts and exclusion counts. Known, unknown and
error results share the same top-level keys, with null values when unavailable.
Publication and logging failures produce the same error schema and preserve the
iteration summary. At most twenty rank
and twenty exclusion rows are published, with omission counts; strings use the
existing 96-byte diagnostic bound. No extra source reads, synchronous DB writes,
order/cancel calls, writer, table or lock order are added by observation.

Each rank's risk check uses the same current account independently, as native
minimum selection does; prior WOULD_SEND ranks are not added to the next rank's
portfolio stress exposure. The prefix cash ledger still deducts their costs.
These values simulate sizing and current per-rank risk checks, not aggregate
post-allocation portfolio stress. They do not simulate C2
U2/cooldown dispatch or guarantee signing, acceptance, fills or net rewards.
Live enablement remains gated by separate C2 work and operator approval: verify
protective cancellation and U2 cancel-all for a full reward day in minimum mode,
then shadow, then an approved trial budget of at most $30.
