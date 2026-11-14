# Over-Head

A wall-mounted live aircraft display for a standard monitor. Over-Head retrieves
live aircraft primarily from ADSB.lol, with ADSB.fi available as a fallback,
and normally selects the nearest aircraft
with a recent position. The browser display is native HTML, CSS and SVG at the
screen's full resolution, while `overhead.py` continues to produce the original
128×64 RGB888 and PNG frame outputs for hardware or integration use.

The monitor is designed to fail closed in live mode. It never silently substitutes
the bundled demonstration aircraft, never guesses a default location, and never
shows an unverified route simply because a callsign has been associated with one
before.

## Live display

The main display shows:

- origin and destination when a trustworthy route is available
- callsign and registration
- aircraft manufacturer/model and ICAO type
- altitude, ground speed, vertical rate and distance from the configured home location
- airline/registered-owner branding when a suitable full wordmark is available
- a live route map with origin, aircraft position and destination
- an optional wider-area radar
- scanning, live, reconnecting, rate-limited, demo and error status

When a successful aircraft response contains no eligible selection, the status
shows **Scanning** and the idle message rotates. Over-Head never inserts sample
traffic during live operation.

When a live response contains no eligible aircraft, commentary observation timing
is skipped until an aircraft is selected. This prevents an `AttributeError` from
interrupting live monitoring when no aircraft qualifies for display.

The browser tab remains branded and follows the displayed route:

```text
Over-Head ✈️ TFS to NCL
```

If an aircraft is present but its route is unknown, the title uses
`N/A to N/A`. With no aircraft it simply shows `Over-Head ✈️`.


## Mobile view

Over-Head has a dedicated narrow-viewport layout rather than shrinking the
desktop two-panel composition.

On phone-sized portrait and landscape viewports, the display is constrained to
a single non-scrolling screen. The normal **Flight** view uses the full panel
width for the route, airline branding and a compact 2×2 metric grid. **Radar**
and **Settings** are alternate full-screen panes: opening Radar replaces the
Flight panel instead of placing it beside it, and the Radar button changes to
**FLIGHT** so the user can switch directly back.

Mobile keeps its own Radar-open preference and does not change the desktop
Radar preference. Starting a persistent TRACK request from the mobile Radar
returns to the Flight pane so the newly locked aircraft is visible immediately;
the tracking lock itself remains active.

The mobile footer is intentionally rendered as three compact acknowledgement
groups with naturally wrapping links. Operational status has its own readable row
above the credits; short screens scroll vertically instead of clipping either row.

## Persistent flight tracking

The radar includes a flight-number/callsign field and **TRACK** control. Enter a
commercial flight number such as `BA703` or an ADS-B/ICAO callsign such as
`BAW703`, then press **TRACK**.

While tracking is active:

- the requested flight overrides normal nearest-aircraft selection
- two-letter IATA flight numbers are mapped to likely ICAO ADS-B callsigns using
  the existing airline index
- the target is fetched directly by callsign instead of requiring it to already
  be inside the local radar query
- the last known tracked aircraft is retained through temporary lookup misses
- the radar range expands and contracts automatically with the aircraft's
  distance from home
- the tracked aircraft remains visible on the radar even when it is beyond the
  local point-query radius
- manual radar `−` and `+` controls are disabled because the scale is automatic
- the **TRACK** button pulses slowly for the duration of the lock
- normal nearest-aircraft updates cannot overwrite the tracked aircraft

Press **TRACK** again with the same flight to stop tracking and restore the
previous manual radar range.

Manual radar distance controls increase or decrease the displayed range in
increments of 5 nautical miles, miles or kilometres, according to the selected
distance unit. Each unit retains its own manual range in the browser. The chosen
range updates immediately using cached aircraft positions, without waiting for
the next upstream aircraft polling interval. Range changes are serialized so
slower network responses cannot undo later clicks, and do not change the normal
aircraft query radius. Automatic tracking adjusts the radar display scale and
the tracking-area query independently.

Normal aircraft monitoring remains restricted to the configured location and
radius. Long-distance tracking uses direct aircraft lookups rather than
expanding the normal geographical query.

## Route resolution

Route data is deliberately separated from aircraft identity data.

When a route is required, Over-Head tries these sources in order:

1. **FlightAware AeroAPI**, when an API key is configured
2. **ADSB.lol routeset**, using the live callsign and aircraft position
3. **ADSB.lol route lookup** as the final free fallback

adsb.im requires prior access approval and is not queried automatically, including
by the service heartbeat. An empty ADSB.lol routeset response (including HTTP 201)
is treated as no route data, not as a JSON error; individual lookups continue.

The original callsign is tried first. If no trustworthy route is found, callsigns
consisting only of an alphabetic prefix and a numeric flight number with redundant
leading zeros are retried without those zeros (for example, `AMX008` -> `AMX8`).
Suffix-bearing identifiers are left unchanged. The alternative must pass the same
live-position validation and is cached under the original callsign + registration;
the aircraft's displayed/transmitted callsign is never changed. Concurrent lookups
for the same identity share the existing serialized cache. ADSB.lol HTTP 403/429
and explicit Retry-After responses pause both route endpoints before further requests.

ADSBDB is **not** used as route authority.

Every candidate route must pass Over-Head's own geographic validation against the
aircraft's current ADS-B position before it is displayed. A provider's own
`plausible` flag is retained for diagnostics but does not override the local
validator. For aircraft below 10,000 ft, an otherwise plausible route is also
rejected when the aircraft is more than 120 NM from both endpoints.

Routes are cached in:

```text
cache/flight-routes-v6.json
```

The cache key is callsign + registration. Positive route entries expire after
15 minutes and are revalidated against the aircraft's current position before
reuse. Failed lookups are retried after 30 seconds.

When no trustworthy route is available, the heading remains a regular-weight,
muted `N/A to N/A`. Over-Head does not invent departure, arrival or remaining
flight times.

For route-resolution diagnostics while the monitor is running:

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/route-debug" |
    ConvertTo-Json -Depth 8
```

See `FLIGHTAWARE_SETUP.txt` for optional AeroAPI configuration and provider
details.

### Persistent route-failure history

Only a visitor's displayed, automatically selected or explicitly tracked aircraft
is recorded after route enrichment completes. Radar-only contacts and transient
loading placeholders are not recorded. No additional provider requests are made.
Missing origin, destination or both are recorded with UTC timestamps, aircraft
identifiers, available airports and bounded provider-attempt diagnostics. No visitor
locations, session identifiers, airport coordinates or API credentials are stored.

Records are grouped by original callsign and aircraft ICAO hex (registration when
hex is unavailable), plus the UTC observation date used as the operating date.
Repeated observations update that record; cache hits and cooldowns do not increase
actual failed-lookup counts. Successful recovery retains prior failures and records
the route/provider. Leading-zero statistics cover actual attempts for logged flights,
not all flights detected by radar.

The optional `route_failure_logging` object in `config.json` configures `path`
(default `cache/route-failures.sqlite3`), `retention_days` (30), `max_records`
(10000) and `history_limit` (50 snapshots per flight). All limits must be positive.
Old records and excess history are pruned; cumulative attempt counters are retained
until the flight record expires. SQLite storage is private to the server and is not
exposed by a browser endpoint. Storage errors are reported at most once per minute
without interrupting monitoring. Run only one monitor process per history file.

Offline report and optional CSV export:

```bash
python route_failures.py --config config.json
python route_failures.py --config config.json --csv route-failures.csv
```

The report lists unresolved/recovered flights, missing-airport counts and leading-zero
fallback success rates. CSV includes the retained provider-attempt history.

## Aircraft identity

For the selected or tracked aircraft, Over-Head uses ADSBDB by registration or
Mode-S address to enrich the live ADS-B record with:

- a more precise aircraft model
- ICAO aircraft type
- registration
- registered owner / airline name
- operator code

Successful identity results are cached in:

```text
cache/aircraft-identities.json
```

If ADSBDB is unavailable or has no matching record, Over-Head continues with the
live aircraft-provider fields and its built-in ICAO type-name table.

## Airport and flag data

FlightAware airport codes are enriched with airport coordinates and country
metadata from the public OurAirports dataset. The dataset is cached locally in:

```text
cache/ourairports-airports.csv
```

and refreshed after seven days when possible.

Known routes display the IATA airport codes using the corresponding national
flag artwork as the fill inside each letter. The route letters use a consistent
visual height and an Over-Head house-grey outline for contrast. Square national
flags such as Switzerland and Vatican City retain their square source
proportions while filling the standard airport-code width.

Country flag artwork is obtained from the MIT-licensed
[country-flag-icons](https://github.com/catamphetamine/country-flag-icons)
project and cached locally.

## Airline branding

Prominent airline branding is intentionally conservative.

Over-Head uses the full self-identifying `logo.svg` wordmark from
[Soaring Symbols](https://github.com/soaring-symbols/soaring-symbols) when one
can be resolved for the aircraft's ICAO operator code. Symbol-only icons and
raster fallbacks are not enlarged into the prominent branding position. If a
suitable wordmark is unavailable, the full airline/registered-owner name is
shown instead when known; otherwise the branding area remains empty.

Successful wordmarks and the airline index are cached in:

```text
cache/logos-v5/
```

Visible artwork is measured in the browser so padded SVG assets can be scaled
consistently without enlarging their transparent margins.

## Route map

A known route with complete airport coordinates enables the large Leaflet route
map.

The map:

- plots origin → current aircraft position → destination
- uses a cyan aircraft icon aligned to the aircraft's live ADS-B track
- keeps airport labels visible at both ends
- centres the complete route rather than the aircraft alone
- animates a persistent ten-second brightness sweep from origin to destination
  to communicate route direction
- applies a brighter centre and four-edge feather so the map blends into the
  surrounding panel
- uses stronger tonal separation between land and water while retaining the
  dark Over-Head appearance

With the radar open, the established map dimensions and framing are preserved.
When the radar is closed, the map uses the extra horizontal space without
increasing its height, moves upward slightly, and uses a tighter vertical route
fit. Its container edges are blended into the main panel so there is no visible
partition between the aircraft data and the map.

The airline wordmark sits in a clean cut-out above the route map and can cross
the main panel border without clipping.

## Header and display behaviour

The header uses the supplied `Over-Head_Logo` asset rather than recreating the
wordmark with a font. The image is preloaded before it becomes visible so a
missing/empty image placeholder cannot flash during startup.

While an aircraft is active, the Over-Head logo becomes a higher-contrast grey.
When a newly selected aircraft triggers the sound alert, the logo briefly returns
to colour as part of the notification.

The top-right controls provide:

- sound mute/unmute
- paired desktop/mobile flight snapshots
- browser full-screen mode
- display settings
- radar show/hide
- live/reconnecting/demo status

When the radar is closed, this control cluster is raised to keep clear of the
airline branding. Radar-open and Settings-open positioning is kept separate.

Double-clicking the display also toggles browser full screen.

## Automatic radar, commentary and photograph rotation

When available, radar and aircraft commentary keep their existing presentation
rotation. The route map and the selected aircraft's photograph share the same
map panel and alternate every 10 seconds. A missing or invalid photograph leaves
the route map visible. Image loading and presentation rotation do not request
additional aircraft-position observations.

## Flight snapshots

The camera control preserves the current aircraft display as a matched pair of PNG
screenshots. One press freezes the current Over-Head flight state and renders both:

- a **1920×1080 desktop** screenshot
- a **390×844 mobile** screenshot

Both images come from the same frozen aircraft payload, route, identity, map state and
radar preference, so the two versions cannot drift onto different live updates while
they are being rendered.

The screenshots are full Over-Head viewport captures. They deliberately retain the
header, controls, complete flight/radar presentation and the full footer/credit line.
If Radar was open when the camera was pressed, the desktop image contains the normal
Flight + Radar composition and the mobile companion contains the mobile Radar view. If
Radar was closed, both variants use their normal Flight presentation.

Visitor snapshot pairs are written beneath a session-specific directory under:

```text
screenshots/visitors/
```

Each visitor can save up to 20 pairs; the server allows at most 50 pairs across
all sessions. Captures are serialized globally, limited to one every 30 seconds
per visitor, and rejected rather than queued when another capture is running.
Files and their index are deleted when that visitor's session expires after 30
minutes without an active browser heartbeat. After a process restart, UUID-named
orphan archives are reconciled at startup and once per minute; only directories
whose modification time is at least 30 minutes old are removed. Recent archives
remain on disk during that retention window and count toward the global snapshot
limit. A returning browser with the same valid opaque cookie can reattach its
recent snapshot archive after restart; its location and tracking state reset to
operator defaults. Cleanup never follows UUID-directory symlinks or removes
entries outside the snapshot root. Filenames include the capture time
and available flight/registration/route identifiers, with `_desktop.png` and
`_mobile.png` suffixes.

Open the saved-snapshots control in the header to browse the archive inside Over-Head.
Each saved flight appears once with Desktop and Mobile previews. Either image can be
opened at full size in the UI, and deleting the record removes both PNG files together.

Snapshot rendering uses Chromium through Playwright so the saved files are genuine
responsive renders of the same Over-Head HTML/CSS/Leaflet interface rather than a
second drawing implementation. The normal setup uses Playwright's pinned Chromium
build, installed as described below. The optional `OVERHEAD_CHROMIUM` environment
variable can instead point to a specific administrator-selected Chromium-compatible
executable.

## Altitude colours

Altitude uses a simple display convention:

- below 5,000 ft: red
- 5,000–14,999 ft: amber
- 15,000 ft and above: green
- unavailable altitude: normal white

These colours are an Over-Head visual convention, not an aviation warning
standard.

## Settings and location

The Settings cog temporarily occupies the same right-hand slot as the radar.
Settings currently provide:

- sound alerts
- current location
- full-screen mode

Closing ordinary Settings restores the radar state that was active before
Settings was opened.

Selecting **CURRENT LOCATION** opens a dedicated Leaflet location chooser that
fits inside the viewport without scrolling. You can:

- use high-accuracy browser geolocation
- click the map
- drag the marker
- review the selected latitude and longitude
- save the new home location

**USE MY LOCATION** gently pulses as `LOCATING...` while the browser location
request is in progress.

Each browser receives an independent, opaque session. Its selected location,
radar range, tracked aircraft and snapshot library are kept in server memory and
are not written to the operator's `config.json`. A session expires after 30
minutes without an active browser heartbeat; refreshing an active page preserves
its session. Browser geolocation is sent to the server for that session and is
discarded when the session expires.

Location changes clear that visitor's previous aircraft and queue a fresh query.
Queries share one server-wide acquisition scheduler, so a change can wait for the
next available provider slot. The status shows when a request is queued. Other
visitors' locations, tracked aircraft and radar ranges are unchanged. Cancelling,
closing, clicking the backdrop or pressing Escape exits the location chooser
back to Radar.

On first normal startup, if `config.json` is missing, Over-Head opens a dedicated
location setup page and does not start live tracking until an explicit location
has been saved.

## Footer and acknowledgements

The live footer begins with:

```text
For Sam 🧡
```

The heart is the live API heartbeat, coloured McLaren Papaya Orange
(`#FF8000`). It reflects the results of requests already made by Over-Head;
there is no independent 30-second API-probing thread. Successful requests
pulse the heart orange. A monitored request failure turns it grey, and a
subsequent successful recovery restores orange and its pulse. API health
indication does not generate extra requests or interrupt the monitor.

FlightAware AeroAPI is not synthetically probed. It is optional and commercial,
so Over-Head does not generate potentially billable AeroAPI requests just to
animate the footer. Normal route-resolution handling remains independent.

The footer keeps linked credits for the services and artwork used in the display:

- ADSB.lol
- ADSB.fi
- FlightAware, only when AeroAPI is configured
- ADSBDB
- OurAirports
- Leaflet
- OpenStreetMap
- Soaring Symbols
- country-flag-icons
- arunangshubanerjee / Pixabay

The footer no longer displays an Inspired-by credit, and also reports the most
recent display update time. On mobile, credits are arranged across three lines.

## Sound

Place `beep-tone.mp3` in the repository root beside `monitor.py`.

Sound is enabled by default and plays when the selected aircraft changes. Use
the bell in the header or the Settings panel to mute or restore it. The choice
is remembered by the browser. A browser may block the first automatic sound
until the page has received a user interaction.

## Install

Python 3.10 or later is required.

### Recommended Linux platform

The reference Linux platform for Over-Head is **Ubuntu 26.04 LTS**.

A clean **Ubuntu 26.04 LTS Server x86_64** installation has been tested from
scratch on DigitalOcean with Python 3.14. The complete clone, virtual-environment
setup, dependency installation and first-run location setup all work without
application-specific system changes.

For a physical machine connected directly to the wall display, Ubuntu 26.04 LTS
Desktop is the more convenient choice because it already provides a graphical
desktop and browser. For a VPS, server or other headless host, Ubuntu Server is
appropriate.

Modern Ubuntu protects the system Python environment, so Over-Head should be
installed inside a Python virtual environment rather than installing packages
directly into the operating system's Python installation.

Install the required system packages:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

Clone Over-Head and create its virtual environment:

```bash
git clone https://github.com/egyptianeyes/Over-Head.git
cd Over-Head

python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
```

The second command installs the Chromium build and Linux runtime dependencies used to
generate the paired flight screenshots. To deliberately use an existing compatible
Chromium/Chrome installation instead, set `OVERHEAD_CHROMIUM` to its executable path.

#### Linux desktop / directly connected display

On a Linux desktop with a graphical browser available:

```bash
python3 monitor.py --open
```

Over-Head opens locally at:

```text
http://127.0.0.1:8765/
```

On the first run, if `config.json` does not exist, Over-Head opens a location
setup screen for the operator default. Saving this persistent default is
restricted to a loopback connection; use the local browser or an SSH tunnel.
Ordinary public visitors choose a separate, temporary session location and
cannot overwrite the operator configuration.

#### Headless Linux server

On a server with no local graphical browser, omit `--open`.

To keep Over-Head private to the server itself:

```bash
python3 monitor.py
```

It will listen on:

```text
http://127.0.0.1:8765/
```

For administration through an SSH tunnel, leave Over-Head bound to localhost and
forward the port from your own computer:

```bash
ssh -L 8765:127.0.0.1:8765 user@SERVER-IP
```

Then open `http://127.0.0.1:8765/` in the browser on your own computer.

If Over-Head is intentionally being made available on the server's network
interfaces, start it with:

```bash
python3 monitor.py --host 0.0.0.0
```

and browse to:

```text
http://SERVER-IP:8765/
```

The built-in HTTP server does not provide TLS or authentication, so port 8765
should **not** be exposed directly to the public Internet. Use an SSH tunnel,
reverse proxy or another appropriately secured access method for remote use.

Press `Ctrl+C` in the terminal to stop Over-Head.

#### Public HTTPS domain on a headless Ubuntu server (Caddy + systemd)

For a public browser display, use a domain such as `demodomain.name` and place
Caddy in front of Over-Head. **Replace `demodomain.name` with your own domain**
throughout these instructions. The service example uses a root-owned code
checkout at `/opt/over-head` and runs the application as the unprivileged
`overhead` account. Commands that install Caddy or configure systemd require
administrative privileges; the application process itself should not run as root.

Before starting, create an **A record** pointing `demodomain.name` to your
server's public IPv4 address (and an AAAA record only if IPv6 is configured and
reachable). Allow inbound **TCP 80 and 443** in the cloud firewall and any host
firewall. Do **not** open port 8765 publicly. HTTPS certificate issuance requires
the domain to resolve to the server and be reachable.

Install Caddy:

```bash
apt update
apt install -y caddy
```

Create `/etc/caddy/Caddyfile`:

```bash
cat > /etc/caddy/Caddyfile <<'EOF'
demodomain.name {
  header {
    Strict-Transport-Security "max-age=31536000"
    X-Content-Type-Options "nosniff"
    Referrer-Policy "strict-origin-when-cross-origin"
    X-Frame-Options "DENY"
  }
    reverse_proxy 127.0.0.1:8765
}
EOF

caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy
```

Run Over-Head with **systemd**, so it starts automatically after a reboot.
**First stop any manually started instance** with `Ctrl+C` in its terminal.
Check that nothing else is listening on port 8765 before starting the service:

```bash
ss -ltnp | grep ':8765' || true
pgrep -af '[m]onitor.py' || true
```

If an old instance is still running, identify and stop that specific process
before proceeding. Running the manual instance and the service together will
cause a port-binding error and repeated service restarts.

The service should not run as root. The example below assumes a root-owned code
checkout at `/opt/over-head`, with only its cache and snapshot directories
writable by a dedicated `overhead` account. The process also keeps its operator
configuration under `/var/lib/over-head`. Adapt these paths before applying them;
this documentation does not change ownership or migrate a live installation.
Install or update the code at `/opt/over-head` using the normal release procedure;
do not copy runtime caches or snapshots as source files. Recreate the virtual
environment at that path and install the already-approved project requirements.

```bash
useradd --system --home /var/lib/over-head --create-home --shell /usr/sbin/nologin overhead
install -d -o overhead -g overhead /var/lib/over-head /opt/over-head/cache /opt/over-head/screenshots
install -o overhead -g overhead -m 0600 /path/to/approved/config.json /var/lib/over-head/config.json
```

For an existing installation, copy the operator's configuration to the state
directory deliberately and preserve any required API-key permissions. Do not
make the source checkout or all of `/opt/over-head` writable by the service.

Create `/etc/systemd/system/over-head.service`:

```bash
cat > /etc/systemd/system/over-head.service <<'EOF'
[Unit]
Description=Over-Head Aircraft Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=overhead
Group=overhead
WorkingDirectory=/opt/over-head
StateDirectory=over-head
ExecStart=/opt/over-head/.venv/bin/python /opt/over-head/monitor.py --host 127.0.0.1 --config /var/lib/over-head/config.json
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/over-head /opt/over-head/cache /opt/over-head/screenshots

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now over-head
```

The application listens **only on localhost**; Caddy serves it to browsers via
`https://demodomain.name/` with automatic HTTPS certificate management. The
application supplies a CSP compatible with its inline UI and pinned Leaflet
assets, plus content-type, referrer, framing and permissions headers. HSTS is
added only by Caddy on the HTTPS site. The CSP uses `unsafe-inline` because the
current application embeds its UI; a nonce/hash migration remains future work.

Verify the services and website:

```bash
systemctl status over-head caddy --no-pager
curl -s -o /dev/null -w 'HTTPS status: %{http_code}\n' https://demodomain.name/
curl -s http://127.0.0.1:8765/status
echo
curl -s http://127.0.0.1:8765/provider-status
echo
```

A successful website request normally returns HTTP `200`. Do not use `curl -I`
to test the application's root URL: it sends an HTTP `HEAD` request, which the
built-in Python server may reject with `501`, even when ordinary browser `GET`
requests work.

If the service fails to start, inspect its logs and check for an existing
listener on port 8765:

```bash
journalctl -u over-head -n 60 --no-pager
ss -ltnp | grep ':8765' || true
journalctl -u caddy -n 60 --no-pager
```

Open `https://demodomain.name/` in an active browser tab to begin monitoring.
When no active display is connected, the status may remain `starting`, with API
request counts at zero; that alone does not indicate a provider failure. The
existing `config.json` is preserved across Git updates. Geographic discovery is
now fixed at 60 seconds, automatic updates at 15 seconds and explicit TRACK
updates at 30 seconds; older
`refresh_seconds` values no longer slow these monitor schedules.

For later application updates, run `git pull --ff-only` from `/root/Over-Head`,
update dependencies inside `.venv` if needed, and restart the service with
`systemctl restart over-head`.

### Windows PowerShell

```powershell
git clone https://github.com/egyptianeyes/Over-Head.git
Set-Location Over-Head
py -m pip install -r requirements.txt
py -m playwright install chromium
py monitor.py --open
```

The browser opens at `http://127.0.0.1:8765/`. Press `Ctrl+C` in the terminal to
stop it.

## Configure live aircraft

On first normal start, if `config.json` does not exist, Over-Head starts a
temporary location-setup service instead of guessing a default position. Open
the displayed URL, choose the home location and press **SAVE LOCATION**. The
saved `config.json` is then used by the normal live monitor.

You can also create or edit `config.json` manually:

```json
{
  "latitude": 51.5074,
  "longitude": -0.1278,
  "radius_nm": 25,
  "refresh_seconds": 60,
  "output_rgb": "output/frame.rgb",
  "output_png": "output/frame.png",
  "demo_on_failure": false
}
```

`radius_nm` sets the normal aircraft query radius and the initial radar display
range. Subsequent manual radar zoom changes only the display range; they do not
change the provider query area. Explicit TRACK mode scales the radar and its
bounded local query automatically while resolving the selected aircraft
directly.

`demo_on_failure` is retained for configuration compatibility, but live operation
does not silently substitute demo traffic. Demonstration aircraft are displayed
only when Over-Head is explicitly started with `--demo`.

### Aircraft polling and provider failover

Normal geographic discovery uses one shared provider-backed slot every **60
seconds**, regardless of the number of visitors: at most 1,440 scans/day before
cooldowns and coverage sharing. Automatically selected aircraft use 15-second
identity-targeted observations, leaving five seconds before the unchanged 20-second
live-position cutoff. Explicit TRACK uses 30-second observations. Lookup identities
remain eligible for refresh for one 60-second discovery cycle, independently of
live display eligibility; this does not keep expired coordinates on the radar. The first automatic
targeted update uses the genuine geographic discovery observation; subsequent
lookups are shared by identity. Inactive sessions are excluded, and distinct
uncovered locations share a fair queue. The service caps concurrent in-memory
sessions at 64 and reports queue position and estimated wait.

An idealized deterministic crossing model illustrates the discovery tradeoff:
for a straight 450-knot track through a 25 NM circle, with path offset uniform
across the circle and poll phase uniform across each interval, expected detection
is about 89.5% at 300 seconds, 98.4% at 120 seconds, 99.5% at 60 seconds and
99.8% at 30 seconds. This is geometry, not observed provider performance; it
assumes a continuous track through the query area and ignores feed coverage,
position age, aircraft traffic patterns and outages.

| Geographic interval | Area scans/day | Worst-case position-provider attempts/day | Modeled crossing detection |
|---:|---:|---:|---:|
| 60 seconds (current) | 1,440 | 2,880 | 99.5% |
| 120 seconds | 720 | 1,440 | 98.4% |
| 30 seconds | 2,880 | 5,760 | 99.8% |

This deterministic crossing model is geometry, not observed provider performance;
it assumes continuous coverage and ignores feed coverage, position age and outages.
The 60-second schedule is implemented, but provider-approved allowances for
these geographic and targeted request rates have not been independently verified.
Browser status polling is not an aircraft observation.

With simultaneous visitors at distinct locations and no reusable geographic
coverage, the deterministic scheduler model is:

| Active visitors | Average first-observation wait | Last first-observation wait | Per-visitor steady cadence | Peak queued visitors |
|---:|---:|---:|---:|---:|
| 1 | immediate | immediate | 1 minute | 0 |
| 2 | 30 seconds | 1 minute | 2 minutes | 1 |
| 5 | 2 minutes | 4 minutes | 5 minutes | 4 |
| 10 | 4 minutes 30 seconds | 9 minutes | 10 minutes | 9 |
| 64 | 31 minutes 30 seconds | 63 minutes | 64 minutes | 63 |

Explicit tracking and automatic selected-aircraft updates have independent
**30-second per-target deadlines**. Explicit TRACK lookup is scheduled promptly
without waiting for the global geographic slot.
Deadlines and shared-cache expiry start at the request attempt, not its response,
so response latency does not silently turn a 30-second interval into 60 seconds.
One acquisition worker serializes requests; network latency, cooldowns and budgets
can still delay updates. Tracking selection survives ordinary updates. Cancellation,
an inactive browser lease (12 seconds), or session expiry stops future targeted
requests unless another active visitor still tracks the aircraft. A request already
in flight cannot be recalled, but a tracking revision check rejects its late result.

Visitors share positive, missing and failed results for the same target interval.
Callsign lookups require an exact match and an unambiguous ICAO identity; once
resolved, subsequent requests are pinned to that hex. Shared callsign results are
short-lived aliases to that observation, not permanent callsign-to-aircraft links.
Distinct hex identities never share history merely because their callsigns match.

All geographic and targeted position attempts, including failed attempts and
fallbacks, consume rolling 60-second budgets: currently **30 per provider and 30
aggregate**, defined by `PROVIDER_REQUESTS_PER_MINUTE` and
`AGGREGATE_REQUESTS_PER_MINUTE` in `overhead.py`. Switching provider does not
reset the aggregate budget. These are local safety ceilings, **not verified
provider-approved allowances**. Operators must establish permitted usage before
live operation and lower these ceilings if necessary; no external provider
research or new providers were introduced by this change. Route, photo, identity
and map services remain separate and are not covered by the position budget.

Thirty-second updates cost at least two successful requests per minute per
distinct target. The aggregate ceiling therefore supports fewer than 15 continuously
tracked targets once geographic scans, failures and fallback overhead are included.
Under overload, least-recently-served targets take priority rather than allowing
early targets to monopolize each renewed budget. This is bounded best-effort
tracking, not a 30-second guarantee under arbitrary load. Budgets and shared caches
are process-local; multiple application instances require a shared limiter.

Deterministic offline workload checks exercise actual mocked upstream requests:

| Workload | Upstream attempts | Observation timing |
|---|---:|---|
| 12 visitors, 4 aircraft, 5 updates over 120 seconds, one primary HTTP 429 | 21 (20 successful lookups + one 429) | Every visitor receives genuine observations at 0/30/60/90/120 seconds |
| 32 distinct aircraft, updates at 0/30/60 seconds | 30/0/30 per update | All targets served by 60 seconds; overloaded updates explicitly delayed |

Zero simulated lookup latency uses instantaneous fixtures and is not an estimate
of real provider latency. A separate eight-second response fixture verifies that
the next permitted request remains due 30 seconds after the previous attempt.

Fresh aircraft observations can be reused for at most 60 seconds when the
queried geographic circle fully contains another visitor's requested circle.
This supports identical locations and genuinely covered overlaps without a new
provider cycle. A normal 25 NM query does not cover a different 25 NM circle
unless their centers coincide, so this does not solve demand across distant
cities. The application currently has no provider-approved budget for increasing
the distinct-location acquisition rate; operator capacity must be kept low or
the provider budget must be established before changing the interval.

The browser refreshes local status every two seconds; those requests do not create
aircraft-provider requests. Provider cooldowns and result sharing reduce actual
traffic, but do not establish that the configured maximum is within upstream
terms.

ADSB.lol is the primary live aircraft-position provider. The existing ADSB.fi
fallback maps the primary's `/v2/point`, `/v2/hex` and `/v2/callsign` paths to
`/api/v3/lat/.../lon/.../dist/...`, `/api/v3/hex/...` and
`/api/v3/callsign/...`. These mappings are tested with local fixtures, not newly
verified against live APIs. Airplanes.live is not implemented. FlightAware,
ADSBDB provides enrichment, not aircraft-position fallback.

Unavailable, forbidden, rate-limited, malformed, missing-target, conflicting or
stale responses can trigger eligible fallback. A satisfactory response stops
additional provider requests. HTTP 403 starts at a five-minute cooldown; network,
timeout, invalid-response and other HTTP failures start at 30 seconds, with
exponential backoff capped at one hour. An explicit `Retry-After`, including an
HTTP date, is never shortened by that cap. Provider cooldowns and budgets are
shared across acquisition modes, and the primary is eligible again after recovery.
The legacy `x-ratelimit-reset` interpretation remains unverified against provider
documentation; a valid `Retry-After` takes precedence.

Normalized records preserve identity, registration, callsign, coordinates, altitude,
speed, heading and vertical rate along with source, receipt time, reported position
age and observation timestamp. An available payload timestamp contributes to the
actual age; HTTP 200 alone does not establish freshness. Geographic selections use
a 20-second freshness threshold; targeted lookups accept observations up to 60
seconds old, but the display labels anything over 20 seconds as last known.
Missing one provider's record is not proof of disappearance. Older responses cannot
overwrite a newer retained target observation. Stale-only responses, lookup misses,
provider failures and delayed acquisition are distinguishable in local status.

Altitude, speed, vertical-rate and distance history use only strictly newer actual
observations, including those shared with other visitors. Source changes retain
history for the same hex; identity changes reset it. Repeated browser status reads,
replayed provider observations and cached updates do not fabricate history entries.
Provider diagnostics read shared local request counters and cooldown state; no
independent health-probe thread makes additional ADS-B requests. FlightAware AeroAPI
is not synthetically probed. When acquisition fails, last-known positions age in
place and expire; the last-success timestamp is not advanced. Live mode never
substitutes demonstration aircraft.

Provider diagnostics are available from the running monitor at
`http://127.0.0.1:8765/provider-status`.
They include active/last-successful provider, last observation and actual age,
request counters, last response status, cooldowns, budget delays and switch reason.
Session `/status` also includes the acquisition interval, observation source and
timestamp, and tracking acquisition outcome.

Validation: `python -m unittest discover` runs the deterministic regression and
workload checks, including two ten-minute clock simulations in
`test_acquisition_continuous.py`. `python acceptance_playwright.py` is the offline
Chromium gate, including 320px, 375px and 390px warning/footer screenshots and
continuous automatic refresh across the freshness boundary. Live API behavior
and provider usage policies require separate production evidence.

Session `/status` exposes bounded `acquisition_diagnostics`: scheduling deadlines,
queue delay, active visitor count, acquisition kind, filtering counts and selected
identity. Provider diagnostics include counts from the latest response. These are
in-memory snapshots, not persistent location logs, and make no additional requests.

Existing installations keep the `refresh_seconds` value in their local
`config.json`: updating code does not overwrite it. The value is retained for
configuration compatibility, while monitor acquisition uses the fixed 60-second
geographic, 15-second automatic and 30-second explicit TRACK schedules. ADSB.lol/ADSB.fi endpoint
availability and provider permission for automatic hex lookups remain
unverified; cooldowns, Retry-After handling and local request ceilings remain
active and may delay these targets.

An optional FlightAware AeroAPI key can be supplied through the
`FLIGHTAWARE_AEROAPI_KEY` environment variable or the `flightaware_api_key`
property in `config.json`. The environment variable takes precedence. Do not
commit a real API key.

See `FLIGHTAWARE_SETUP.txt` for details.

For a dedicated Linux display machine, Chromium can be launched in kiosk mode
after Over-Head is running:

```bash
chromium --kiosk --noerrdialogs --disable-infobars http://127.0.0.1:8765/
```

A headless server does not need Chromium for the live display itself, but the paired
flight-snapshot feature does require the Playwright Chromium installation above (or an
administrator-selected browser supplied through `OVERHEAD_CHROMIUM`).

## Standalone RGB output

`overhead.py` remains the selection/rendering layer for the original 128×64
RGB888 output.

Run once:

```bash
python3 overhead.py
```

or continuously:

```bash
python3 overhead.py --watch
```

Every render writes the same frame in two formats:

- `output/frame.rgb`: raw, tightly packed RGB888 bytes, row-major from the top left
- `output/frame.png`: directly viewable PNG preview

The raw frame is exactly 24,576 bytes: `128 × 64 × 3`.

During long-distance browser tracking, the local ADSB.lol point query is capped
at 250 NM. This does not limit the direct callsign lookup used by persistent
TRACK mode.

## Test

```bash
python3 -m unittest -v
```

The regression suite covers route-source selection and plausibility,
persistent flight tracking, route-map layout and animation, branding, flags,
location handling, radar behaviour, browser title state, paired flight snapshots,
footer credits, live/demo failure behaviour and the RGB output path.

Run the offline desktop/mobile browser acceptance checks with
`python3 acceptance_playwright.py`. They use fixture aircraft data and a local
Leaflet shim; they do not contact aircraft or map services. Playwright Chromium
and its Linux system libraries must be installed for these checks to run.

## Connecting RGB hardware later

The raw RGB888 output can be consumed by a panel driver that maps the 128×64
frame to HUB75 panels, addressable RGB tiles, SDL, Linux framebuffer or another
controller without changing the browser monitor.

## Author

Over-Head is an [Egyptian Eyes](https://egyptianeyes.com) project by
[20tele](https://20tele.com).

## Acknowledgements

Live ADS-B aircraft positions and direct callsign tracking are provided
primarily by the [ADSB.lol](https://adsb.lol/) community network, with
[ADSB.fi](https://opendata.adsb.fi/) used as an alternative source when needed.

Free route fallback data is queried from [ADSB.lol](https://adsb.lol/)
and is independently checked against the current
aircraft position before display.

Optional commercial-flight resolution is provided by
[FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi/) when
configured.

Aircraft identity and registered-owner enrichment is provided by
[ADSBDB](https://www.adsbdb.com/). ADSBDB is not used as route authority.

Airport coordinate and country metadata is obtained from the public
[OurAirports](https://ourairports.com/data/) dataset.

The first-run location selector and live route map use
[Leaflet](https://leafletjs.com/) and
[OpenStreetMap](https://www.openstreetmap.org/copyright).

Prominent airline wordmarks are retrieved from
[Soaring Symbols](https://github.com/soaring-symbols/soaring-symbols).
Airline names, marks and logos remain the property of their respective owners
and are not covered by Over-Head's MIT licence.

Country flag artwork is provided by the MIT-licensed
[country-flag-icons](https://github.com/catamphetamine/country-flag-icons)
project.

`beep-tone.mp3` was created by arunangshubanerjee and sourced from
[Pixabay](https://pixabay.com/) under the
[Pixabay Content License](https://pixabay.com/service/terms/). The sound effect
is not covered by Over-Head's MIT License.

Over-Head is an independent project and is not affiliated with or endorsed by
ADSB.lol, ADSB.fi, adsb.im, FlightAware, ADSBDB, OurAirports, Soaring Symbols,
country-flag-icons, Pixabay or any airline.

## License

Over-Head is released under the [MIT License](LICENSE).
