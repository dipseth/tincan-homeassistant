# Tin Can API reference

All examples below are **anonymized** (fake names/numbers/ids) but the **field names and shapes are real**.
Base host for data: `https://api.tincan.com`. Auth host: `https://archie.tincan.kids`.
Every data request needs `Authorization: Bearer <WP-JWT>`. Transport is TLS 1.3 / HTTP/1.1.
Responses wrap payloads in `data` and carry a `meta: { timestamp, request_id }` (omitted below for brevity).

Observed request headers from the app (not all required): `user-agent: Dart/3.11 (dart:io)`, `accept-encoding: gzip`,
`x-app-version: 3.1.2+433`, `x-app-platform: android`.

---

## 1. Login — `POST https://archie.tincan.kids/wp-json/jwt-auth/v1/token`

Body `{ "username": "<email>", "password": "<password>" }` →

```json
{ "token": "<HS256 JWT, ~60 min>", "refresh_token": "<opaque>",
  "user_email": "you@example.com", "user_display_name": "..." }
```

The JWT payload decodes to `{ iss: "https://archie.tincan.kids", iat, nbf, exp, data: { user: { id, customer_id, public_id, ... } } }`.
`exp - iat ≈ 3600`. Use the token as the Bearer for everything in sections 2–8.

## 1b. Refresh — `POST https://archie.tincan.kids/wp-json/jwt-auth/v1/token/refresh`

Body `{ "refresh_token": "<opaque>" }` → `{ "token": "<fresh JWT>", "refresh_token": "<maybe rotated>" }`.
No password needed. On failure, do a full login.

---

## 2. Subscriptions — `GET /subscriptions`

The account's Tin Can lines. Iterate these; `subscription_id` keys everything else. Also the source of **DND** (`dnd_enabled`)
and the line's **phone number / caller_id**.

```json
{ "data": [ {
  "subscription_id": "11111111-2222-3333-4444-555555555555",
  "phone_number": "+15551230000", "phone_number_id": "PN...", "provider": "twilio",
  "internal_id": "900000", "type": 2, "is_v2": true,
  "caller_id": "Robin", "avatar": "https://cdn.example/avatar.png",
  "dnd_enabled": false, "emergency_enabled": true, "emergency_status": "enabled",
  "onboarding_complete": true, "primary_device_type": "TINCAN",
  "emergency_address": { "street": "1 Main St", "city": "Springfield", "region": "IL",
                         "postal_code": "60000", "country": "US", "status": "registered" },
  "stripe_subscription_id": "sub_...", "created_at": "2025-12-01T00:00:00.000Z", "archived_at": null
} ] }
```

## 3. Call log (CDRs) — `GET /subscription/{subscription_id}/cdrs`

Query: `page=1&limit=50&sort_by=start_time&order=desc`. `records[0]` = most recent. Paginated (`total`, `totalPages`).
**This is the live-ish call/caller-ID source.** A record appears within ~seconds of a call.

```json
{ "data": {
  "records": [
    { "call_id": "abc123@0.0.0.0", "call_type": "incoming",
      "start_time": "2026-10-20T14:34:10.880Z", "duration": 26, "status": "completed",
      "from_routing_id": "700000", "from_number": null, "from_name": "Alex Doe",
      "from_avatar": "https://cdn.example/lightning.png",
      "to_routing_id": null, "to_number": null, "to_name": null, "to_avatar": null,
      "other_party_is_tincan": true },
    { "call_id": "def456@0.0.0.0", "call_type": "outgoing",
      "start_time": "2026-10-20T14:20:03.115Z", "duration": 0, "status": "missed",
      "from_routing_id": null, "from_number": null, "from_name": null, "from_avatar": null,
      "to_routing_id": null, "to_number": "+15557654321", "to_name": "Grandma",
      "to_avatar": "https://cdn.example/smiley.png", "other_party_is_tincan": false }
  ],
  "total": 183, "page": 1, "totalPages": 4, "limit": 50
} }
```

- `call_type`: `incoming` | `outgoing` | `missed`. `status`: `completed` | `missed` | `voicemail`.
- "Other party" = `from_*` for `incoming`, else `to_*`. Match it to the contact book (section 4) via
  `*_routing_id` (== a contact number's `dial_plan`) first, then `*_number` (== `phone_number`, compare last 10 digits).
- Direction can look inverted for inbound calls from saved contacts (logged as `outgoing` with the contact under `to_*`).
  The contact match still resolves the right person; don't rewrite the label.

## 4. Approved contacts — `GET /subscription/{subscription_id}/contacts`

The kid's real approved contact book. **Use this, not `/contacts?q=`** (that is a global directory of all Tin Can users).

```json
{ "data": { "contacts": [ {
  "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Alex Doe",
  "status": "approved", "enabled": true, "avatar": "https://cdn.example/lightning.png",
  "subscription_id": "11111111-2222-3333-4444-555555555555", "subscription_type": 2,
  "contact_numbers": [ {
    "key": "...-uuid", "label": "tincan", "phone_number": "+15550001111",
    "dial_plan": "700000", "group_id": "...-uuid",
    "request_status": "approved", "favorite": "speeddial_1", "enabled": true,
    "created_at": "...", "updated_at": "...", "archived_at": null
  } ],
  "created_at": "...", "updated_at": "...", "archived_at": null
} ] } }
```

- `dial_plan` is the routing id that CDRs reference as `from_routing_id`/`to_routing_id`.
- `favorite` encodes speed-dial slot (e.g. `speeddial_1`), `null` if none. `avatar` is the per-contact picture.

## 5. Devices — `GET /account/devices`

```json
{ "data": [ {
  "device_id": "...-uuid", "name": "Robin's Tin Can", "mac_address": "dc:b4:d9:xx:xx:xx",
  "device_type": "TINCAN", "provisioning_status": "provisioned",
  "avatar": "https://cdn.example/tin-can-icon.png",
  "subscription_id": "11111111-2222-3333-4444-555555555555",
  "created_at": "...", "updated_at": "..." } ] }
```

## 6. Presence — `GET /device/status?device_type=TINCAN&mac_address=<mac>`

Both query params required; `device_type` must be UPPER-CASE (`TINCAN` or `HT801`). Connectivity only — **does not change
during a call**.

```json
{ "data": { "device_status": "online", "is_claimed": true,
            "mac_address": "dc:b4:d9:xx:xx:xx", "device_type": "TINCAN" } }
```

## 7. Availability — `GET /subscription/{subscription_id}/availability`

The line's accept-calls / scheduled-quiet flag (NOT live call state).

```json
{ "data": { "availability": true, "until": null } }
```

## 8. Time conditions — `GET /subscription/{subscription_id}/time-conditions`

Scheduled DND / allowed-hours rules (quiet-time schedule). Lower priority; inspect live if you want to surface a schedule.

---

### Endpoint cheat-sheet

| Purpose | Method + path | Notes |
|---|---|---|
| Login | `POST archie.tincan.kids/wp-json/jwt-auth/v1/token` | email+pw → JWT (+refresh) |
| Refresh | `POST archie.tincan.kids/wp-json/jwt-auth/v1/token/refresh` | refresh_token → JWT |
| Lines | `GET /subscriptions` | `data[].subscription_id`, DND, phone number |
| Call log | `GET /subscription/{id}/cdrs?page=1&limit=50&sort_by=start_time&order=desc` | caller-ID / "recent call" |
| Contacts | `GET /subscription/{id}/contacts` | approved book (`data.contacts[]`) |
| Devices | `GET /account/devices` | device_id, mac, provisioning |
| Presence | `GET /device/status?device_type&mac_address` | online/offline only |
| Availability | `GET /subscription/{id}/availability` | DND/schedule flag |
| Schedule | `GET /subscription/{id}/time-conditions` | quiet-hours rules |
