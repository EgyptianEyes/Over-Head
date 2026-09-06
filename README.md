# Over-Head

A wall-mounted live aircraft display for a standard monitor. Over-Head retrieves
live aircraft from the ADSB.lol network and normally selects the nearest aircraft
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
- live, reconnecting and demo status

When no aircraft is available, the display shows **Scanning the skies…** rather
than inserting sample traffic.

The browser tab remains branded and follows the displayed route:

```text
Over-Head ✈️ TFS to NCL
```

If an aircraft is present but its route is unknown, the title uses
`N/A to N/A`. With no aircraft it simply shows `Over-Head ✈️`.

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

Manual radar ranges are 5, 10, 15, 25, 40, 60 and 100 NM. Automatic tracking can
use substantially larger ranges for distant flights. The ordinary ADSB.lol point
query is capped at 250 NM so long-distance tracking does not turn the local
traffic query into an unnecessarily large request.

## Route resolution

Route data is deliberately separated from aircraft identity data.

When a route is required, Over-Head tries these sources in order:

1. **FlightAware AeroAPI**, when an API key is configured
2. **adsb.im routeset**, using the live callsign and aircraft position
3. **ADSB.lol routeset**, using the live callsign and aircraft position
4. **ADSB.lol route lookup** as the final free fallback

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
live ADSB.lol fields and its built-in ICAO type-name table.

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
- browser full-screen mode
- display settings
- radar show/hide
- live/reconnecting/demo status

When the radar is closed, this control cluster is raised to keep clear of the
airline branding. Radar-open and Settings-open positioning is kept separate.

Double-clicking the display also toggles browser full screen.

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

Saving the location preserves other `config.json` values, updates the running
display immediately, and returns directly to Radar. Cancelling, closing,
clicking the backdrop or pressing Escape also exits the location chooser back to
Radar.

On first normal startup, if `config.json` is missing, Over-Head opens a dedicated
location setup page and does not start live tracking until an explicit location
has been saved.

## Footer and acknowledgements

The live footer begins with:

```text
For Sam 🧡
```

and keeps an intentionally expansive set of linked credits for the services and
artwork visible in the display:

- ADSB.lol
- FlightAware, only when AeroAPI is configured
- ADSBDB
- OurAirports
- Leaflet
- OpenStreetMap
- Soaring Symbols
- country-flag-icons
- arunangshubanerjee / Pixabay
- TheFlightWall

The footer also reports the most recent display update time.

## Sound

Place `beep-tone.mp3` in the repository root beside `monitor.py`.

Sound is enabled by default and plays when the selected aircraft changes. Use
the bell in the header or the Settings panel to mute or restore it. The choice
is remembered by the browser. A browser may block the first automatic sound
until the page has received a user interaction.

## Install

Python 3.10 or later is recommended.

### Linux

```bash
git clone https://github.com/egyptianeyes/Over-Head.git
cd Over-Head
python3 -m pip install -r requirements.txt
python3 monitor.py --open
```

### Windows PowerShell

```powershell
git clone https://github.com/egyptianeyes/Over-Head.git
Set-Location Over-Head
py -m pip install -r requirements.txt
py monitor.py --open
```

The browser opens at `http://127.0.0.1:8765/`. Press `Ctrl+C` in the terminal to
stop it.

## Configure live aircraft

On first normal start, if `config.json` does not exist, use the location setup
screen to save the home position.

You can also create or edit `config.json` manually:

```json
{
  "latitude": 51.5074,
  "longitude": -0.1278,
  "radius_nm": 25,
  "refresh_seconds": 10,
  "output_rgb": "output/frame.rgb",
  "output_png": "output/frame.png",
  "demo_on_failure": false
}
```

`demo_on_failure` is retained for configuration compatibility, but live operation
does not silently substitute demo traffic. Demonstration aircraft are displayed
only when Over-Head is explicitly started with `--demo`.

An optional FlightAware AeroAPI key can be supplied through the
`FLIGHTAWARE_AEROAPI_KEY` environment variable or the `flightaware_api_key`
property in `config.json`. The environment variable takes precedence. Do not
commit a real API key.

See `FLIGHTAWARE_SETUP.txt` for details.

For a dedicated monitor, Chromium can be started in kiosk mode:

```bash
chromium --kiosk --noerrdialogs --disable-infobars http://127.0.0.1:8765/
```

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

The current regression suite covers route-source selection and plausibility,
persistent flight tracking, route-map layout and animation, branding, flags,
location handling, radar behaviour, browser title state, footer credits,
live/demo failure behaviour and the RGB output path.

## Connecting RGB hardware later

The raw RGB888 output can be consumed by a panel driver that maps the 128×64
frame to HUB75 panels, addressable RGB tiles, SDL, Linux framebuffer or another
controller without changing the browser monitor.

## Author

Over-Head is an [Egyptian Eyes](https://egyptianeyes.com) project by
[20tele](https://20tele.com).

## Acknowledgements

Over-Head was inspired by
[TheFlightWall](https://github.com/AxisNimble/TheFlightWall_OSS), an open-source
aircraft information display created by AxisNimble.

Live ADS-B aircraft position/state and direct callsign tracking are provided by
the [ADSB.lol](https://adsb.lol/) community network.

Free route fallback data is queried from [adsb.im](https://adsb.im/) and
[ADSB.lol](https://adsb.lol/) and is independently checked against the current
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
TheFlightWall, ADSB.lol, adsb.im, FlightAware, ADSBDB, OurAirports, Soaring
Symbols, country-flag-icons, Pixabay or any airline.

## License

Over-Head is released under the [MIT License](LICENSE).
