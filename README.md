<p align="center">
  <img src="custom_components/tincan/brand/icon@2x.png" alt="Tin Can icon" width="128" height="128">
</p>
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/tincan/brand/dark_logo@2x.png">
    <img src="custom_components/tincan/brand/logo@2x.png" alt="Tin Can" width="320">
  </picture>
</p>

# Tin Can for Home Assistant

[![Release](https://img.shields.io/github/v/release/dipseth/tincan-homeassistant)](https://github.com/dipseth/tincan-homeassistant/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)

Bring the [Tin Can](https://tincan.kids) kids phone into Home Assistant. Sign in with your parent account and
every Tin Can line on it shows up as a device: online status, Do Not Disturb, who called last, a near-live
"Recent Call" flag, the approved contact list and more. Read-only; nothing on your account is ever changed.

## Features

- **Caller ID** with the contact's name and avatar, matched against the phone's approved contact book
- **Call State** sensor (`idle` / `ringing` / `in_call`) that follows a call while it happens
- **Recent Call** binary sensor that turns on as soon as a call ends or is missed, and off shortly after,
  ready for "flash the lights when Grandma calls" automations
- **`tincan_call` event** on the Home Assistant event bus once per call, when it has ended or been missed
- **Online** and **Do Not Disturb** status, phone number, subscription and provisioning details
- Multiple lines on one account are supported; each becomes its own device
- Re-authentication prompt if the password changes; polling intervals adjustable from the UI

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/dipseth/tincan-homeassistant`, type *Integration*
2. Install **Tin Can** from HACS and restart Home Assistant
3. **Settings → Devices & services → Add integration → Tin Can** and sign in with the parent email and password

### Manual

Copy `custom_components/tincan` into your Home Assistant `config/custom_components/` folder and restart.
`install.sh` does this over SSH.

## Entities

One device per Tin Can line, named after the phone:

| Entity | Type | Notes |
|---|---|---|
| **Online** | binary sensor (connectivity) | whether the phone is connected to the Tin Can cloud |
| **Do Not Disturb** | binary sensor | the line's DND setting |
| **Call State** | sensor (enum) | `idle`, `ringing` or `in_call`; caller picture + call attributes while a call is live |
| **Recent Call** | binary sensor | on for *N* seconds after the newest call ends or is missed (default 30); caller picture + call attributes |
| **Last Call** | sensor | `<who> (<direction>, <status>)` once over, `<who> (<direction>, ringing / in call)` before; caller avatar as picture, full call attributes |
| **Last Call Time** | sensor (timestamp) | start of the newest call |
| **Phone Number** | sensor | the line's number; provider and emergency address as attributes |
| **Approved Contacts** | sensor | count, with `names` and `speeddials` attributes |
| **Subscription** | sensor | active / archived, with plan and emergency details; the kid's avatar as picture |
| **Provisioning** | sensor (diagnostic) | device provisioning status, MAC address, device id |

### Call attributes

Last Call, Recent Call and the `tincan_call` event share these attributes:

| Attribute | Meaning |
|---|---|
| `other_party_name` | contact name if matched, else the name or number the API reported |
| `other_party_number`, `routing_id` | the other party's number / Tin Can routing id |
| `direction` | `incoming`, `outgoing` or `unknown`, from the kid's phone's point of view |
| `phase` | `ringing`, `in_call`, `ended` or `missed` (see *Call lifecycle* below) |
| `status`, `missed`, `duration` | raw API status (`completed` / `missed` / `voicemail`); `missed` is true only once an unanswered call is over; `duration` is talk time |
| `start_time`, `end_time` | ISO timestamps; `end_time` is empty until the call is over |
| `contact_name`, `contact_id`, `speeddial`, `group_id`, `number_label` | from the matched approved contact |
| `avatar` | the contact's picture (also used as the entity picture) |
| `other_party_is_tincan` | whether the other party is another Tin Can |
| `api_call_type`, `direction_source` | the raw API label and whether `call_id` or `call_type` decided the direction |

**How direction is determined.** Calls that reach the phone from a regular phone number arrive through the
SIP carrier and are labelled `outgoing` by the API, but their call id is SIP-style (`...@host`), so that
format wins and they count as incoming. All other records are labelled correctly. The rule was verified with
controlled test calls against the full call history.

### Example automation

```yaml
automation:
  - alias: Flash the hall light when someone calls the Tin Can
    triggers:
      - trigger: event
        event_type: tincan_call
        event_data:
          direction: incoming
    actions:
      - action: light.turn_on
        target:
          entity_id: light.hall
        data:
          flash: short
      - action: notify.mobile_app_phone
        data:
          message: "{{ trigger.event.data.other_party_name }} called the Tin Can"
```

## Configuration

Everything is set up from the UI. Under the integration's **Configure** button:

| Option | Default | Notes |
|---|---|---|
| Account poll interval | 30 s | presence, DND, contacts, subscription |
| Call log poll interval | 8 s | how quickly a new call is noticed |
| Recent Call window | 30 s | how long Recent Call stays on after a call ends |

The call log poll drives Call State, Recent Call and the `tincan_call` event, so they lag the phone by up to
one call log poll interval.

### Call lifecycle

The Tin Can cloud writes a call's record the moment the phone starts ringing and rewrites it as the call goes
on, so a single snapshot can't tell a ringing call from a missed one. Observed with controlled test calls:

| | Ringing | Then | Finally |
|---|---|---|---|
| Answered | `missed`, 0 s | `completed`, 0 s while talking | `completed`, *N* s (talk time) |
| Not answered | `missed`, 0 s | `completed`, 0 s at ~48 s while ringing out | `missed`, 0 s at ~64 s |

The integration follows each call across polls: a `missed` record counts as final only after the call has
been seen at `completed`, or once it is older than 75 s (for example when the caller hangs up early). A call
can also be logged several times with different call ids but the same start time; those records are merged
into one call. An unanswered call reads `in_call` for about 15 s while it rings out before settling as missed.

## How it works

The integration logs in to the Tin Can parent API with your email and password, receives a short-lived token
plus a refresh token, and renews the token in the background. Only read endpoints are used: the account's
lines and devices, each line's approved contacts and call log, and device presence. See [API.md](API.md) for
the endpoint reference. `tc_api.py` is a small command-line client for the same API, handy for poking at it.

## Development

```bash
uv venv -p 3.14 .venv && uv pip install -p .venv/bin/python pytest-homeassistant-custom-component
.venv/bin/python -m pytest      # unit tests always run; live tests need TC_EMAIL / TC_PASSWORD in .env
```

The live tests boot the integration inside a test Home Assistant instance against a real account.

## Disclaimer

Not affiliated with Tin Can. Uses the same cloud API as the Tin Can app; if the API changes, this
integration may stop working until updated.
