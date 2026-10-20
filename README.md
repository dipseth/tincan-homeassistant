# Tin Can for Home Assistant

A native Home Assistant integration for the [Tin Can](https://tincan.kids) kids phone. It polls the Tin Can
cloud with your parent login and exposes the phone as a device with sensors. No MQTT, no bridge process,
nothing to run on another machine. Read-only: it never changes anything on your account.

## What you get

One device per Tin Can line (named after the phone, e.g. "Robin's Tin Can") with:

| Entity | Kind | Notes |
|---|---|---|
| **Online** | binary_sensor (connectivity) | from `/device/status`; does **not** flip during a call |
| **Do Not Disturb** | binary_sensor | `dnd_enabled` on the subscription |
| **Recent Call** | binary_sensor | on for *N* s after the newest call **ends** (default 30 s); caller picture + attributes |
| **Last Call** | sensor | `"<who> (<direction>, <status>)"`, caller avatar as entity picture, rich attributes |
| **Last Call Time** | sensor (timestamp) | start of the newest call |
| **Phone Number** | sensor | the line's number; provider / emergency address as attributes |
| **Approved Contacts** | sensor | count; `names` and `speeddials` attributes |
| **Subscription** | sensor | active / archived; plan + emergency flags as attributes |
| **Provisioning** | sensor (diagnostic) | device provisioning status, MAC, device id |

It also fires a **`tincan_call` event** on the HA event bus every time a new call record appears, with the
same attributes as Last Call (`other_party_name`, `direction`, `status`, `duration`, `contact_name`,
`speeddial`, `avatar`, ...). Use it as an event trigger in automations.

### Call direction

`direction` is **incoming / outgoing / unknown** from the kid's phone's point of view. Verified with controlled
test calls on 2026-10-20 against the full call history:

- A regular phone ringing the Tin Can arrives through the SIP carrier. Its record is labelled `outgoing` with the
  caller under `to_*`, but its `call_id` is SIP-style (`...@host`). That format wins: **`@` in the call id means
  incoming.** Tin Can-to-Tin Can calls and calls the kid dials never have one.
- Every other record is labelled correctly: `incoming` / `missed` carry the caller under `from_*` (including
  calls placed from the parent app), `outgoing` carries the callee under `to_*`.

Attributes: `api_call_type` is the raw label, `direction_source` says whether `call_id` or `call_type` decided,
and `missed` is true for missed / voicemail records in either direction. A call the kid dials out to a regular
phone produces two records with the same start time (UUID + token ids); both are the same call.

## Install (personal use, no HACS needed)

```bash
./install.sh                # copies custom_components/tincan to the HA box and restarts Core
```

The script uses the `homeassistant` host from `~/.ssh/config`; pass another ssh target as the first argument.
Then **Settings → Devices & services → Add integration → "Tin Can"** and sign in with the parent email and
password. Polling intervals and the Recent Call window are under the integration's **Configure** button.

### Replacing the MQTT bridge

The old `tc2mqtt.py` bridge produced a device with the **same name and the same entity ids**. To avoid
`_2` suffixes:

1. Stop the bridge on the Mac: `launchctl unload ~/Library/LaunchAgents/com.tincan.ha2mqtt.plist`
2. In HA, delete the MQTT device "Robin's Tin Can" (Settings → Devices → the device → ⋮ → Delete).
3. Add the Tin Can integration. Existing automations keep working because the entity ids match.

### Icon in Home Assistant

The Tin Can icon and logo ship inside the integration (`custom_components/tincan/brand/`, supported since
Home Assistant 2026.3), taken from the official app artwork: the yellow smiley as the icon, the black wordmark
for light theme and the yellow one for dark theme. Entity pictures (caller avatars, the kid's avatar) come
straight from the API.

### HACS

HACS → ⋮ → **Custom repositories** → add `https://github.com/dipseth/tincan-homeassistant` with type
*Integration*, then install "Tin Can" from HACS and restart Home Assistant. HACS will offer updates whenever a
new release is tagged here.

## Development

```bash
uv venv -p 3.14 .venv && uv pip install -p .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest          # unit test always runs; live tests need TC_EMAIL / TC_PASSWORD in .env
```

`tc_api.py` is the synchronous reference client (handy for poking the API from a shell), `API.md` documents
every endpoint, and `reference/tc2mqtt.py` is the retired MQTT bridge kept for comparison.

## Auth model

1. `POST https://archie.tincan.kids/wp-json/jwt-auth/v1/token` `{username, password}` → JWT (~60 min) + refresh token
2. `Authorization: Bearer <jwt>` against `https://api.tincan.com`
3. `POST .../token/refresh` `{refresh_token}` renews without the password; the refresh token is stored in the
   config entry. Full login is the fallback, and a rejected password triggers HA's re-authentication prompt.
