#!/usr/bin/env python3
"""tc2mqtt.py — Tin Can -> Home Assistant via MQTT discovery (read-only, auto-reconnect).

Polls the Tin Can CURRENT cloud API (api.tincan.com, see tc_api.py) with the WP-JWT
Bearer and publishes Home Assistant MQTT-discovery sensors for each Tin Can phone.

IMPORTANT (2026-10-19): the old /v2 API is FROZEN for this account since ~June 24, so
this bridge now reads the live api.tincan.com routes that the Bearer token can access:
  GET /account/devices    device list (uuid, name, mac, provisioning) -- LIVE
  GET /subscriptions      phone number / DID, plan, emergency address  -- LIVE
  GET /subscription/{id}/contacts   approved contact book (names/avatars/speeddial) -- LIVE
  GET /subscription/{id}/cdrs       call log / caller-ID (Last & Recent Call)        -- LIVE
  GET /device/status?device_type&mac_address   online/offline presence -- LIVE (Bearer)
Online/offline presence, Do Not Disturb, AND the call log / caller-ID (CDRs) are ALL
published with the WP-JWT Bearer. The CDR route (GET /subscription/{id}/cdrs) was
recovered by runtime capture on 2026-10-20 — earlier handoffs wrongly believed call data
was gated behind AWS IAM SigV4 (api.tincan.com /calls) or a Firebase idToken in the
account firestore_uid ACL (v3 gateway); it is not. /device/status reflects connectivity
only — it does NOT change during a call (verified on a live call); the Last Call sensor
polls the CDR list every TC_POLL seconds. Stale v2 sensors (Discoverable, Status) are
DELETED from HA on startup.

Published sensors per phone:
  binary_sensor Online          connectivity from /device/status (Bearer)
  binary_sensor Do Not Disturb  dnd_enabled from /subscriptions
  sensor  Phone Number     the DID the phone presents (+ provider / emergency addr attrs)
  sensor  Provisioning     device provisioning_status (+ uuid / mac / name attrs)
  sensor  Approved Contacts contact count (+ names list attr)
  sensor  Subscription     plan / status (+ caller_id / avatar / dnd / emergency attrs)
  sensor  Last Call        latest CDR "<party> (<dir>, <status>)" (+ caller-ID attrs;
                           entity_picture = caller avatar)
  sensor  Last Call Time   timestamp of the latest call (device_class: timestamp)
  sensor  Data Source      which API + capabilities note

Run:
  pip install paho-mqtt
  MQTT_HOST=10.0.0.93 MQTT_USER=mqtt MQTT_PASS=secret python3 tc2mqtt.py

Env: MQTT_HOST (default homeassistant.local), MQTT_PORT (1883), MQTT_USER, MQTT_PASS,
     TC_POLL (full poll, default 30), TC_FAST_POLL (CDR-only ring poll, default 8),
     TC_RING_WINDOW (Recent Call ON window s, default 90); plus TC_EMAIL / TC_PASSWORD in .env.
"""
import json, os, sys, time, socket, threading, urllib.parse
from datetime import datetime, timezone, timedelta

socket.setdefaulttimeout(25)        # bound any socket op so a dead link can't hang us
_HEARTBEAT = {"ok": time.time()}    # last successful poll; watched by the watchdog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tc_api

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt not installed. Run:  pip install paho-mqtt")

MQTT_HOST = os.environ.get("MQTT_HOST", "homeassistant.local")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
POLL      = int(os.environ.get("TC_POLL", "30"))
RING_WINDOW = int(os.environ.get("TC_RING_WINDOW", "30"))  # "Recent Call" ON window after a call ENDS (s)
FAST_POLL = int(os.environ.get("TC_FAST_POLL", "8"))       # CDR-only poll for low-latency ring

DISC  = "homeassistant"          # HA discovery prefix
BASE  = "tincan"                 # state topic base
AVAIL = f"{BASE}/status"

# stale v2 entities to remove from HA (publish empty retained config).
# NOTE: "online" is NOT stale anymore — it is republished live from /device/status
# (Bearer-OK as of 2026-10-19); only DND/discoverable/call data remain unreachable.
STALE = [("binary_sensor", "discoverable"),
         ("sensor", "status")]
_cleaned = set()
_TARGETS = []            # [(did, subscription_id)] polled by the fast CDR loop; set by poll_once
_CONTACTS_BY_SUB = {}    # subscription_id -> contact book (for caller matching); set by poll_once


def _digits(s):
    """Last 10 digits of a phone number, for matching +1XXXXXXXXXX vs 1XXXXXXXXXX vs XXXXXXXXXX."""
    return "".join(ch for ch in str(s or "") if ch.isdigit())[-10:]


def _contact_index(contacts):
    """Map routing-id (dial_plan) and phone number -> a compact contact-book summary, so a CDR's
    other party can be matched back to the approved contact (name, avatar, speeddial, group)."""
    idx = {}
    for c in contacts or []:
        base = {"contact_name": c.get("name"), "contact_avatar": c.get("avatar"),
                "contact_id": c.get("id"), "contact_status": c.get("status")}
        for n in c.get("contact_numbers") or []:
            s = dict(base, speeddial=n.get("favorite"), dial_plan=n.get("dial_plan"),
                     phone_number=n.get("phone_number"), number_label=n.get("label"),
                     group_id=n.get("group_id"))
            if n.get("dial_plan"):
                idx[str(n["dial_plan"])] = s
            if n.get("phone_number"):
                idx[_digits(n["phone_number"])] = s
    return idx


def _client():
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        c = mqtt.Client()
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.will_set(AVAIL, "offline", retain=True)
    return c


def _device_payload(dev_id, name):
    return {"identifiers": [f"tincan_{dev_id}"], "name": name or f"Tin Can {dev_id}",
            "manufacturer": "Tin Can", "model": "WP816"}


def _clean_stale(cli, did):
    """Delete the old v2 sensors from HA by publishing empty retained config payloads."""
    if did in _cleaned:
        return
    for kind, key in STALE:
        cli.publish(f"{DISC}/{kind}/tincan_{did}/{key}/config", "", retain=True)
        cli.publish(f"{BASE}/{did}/{key}", "", retain=True)
    _cleaned.add(did)
    print(f"cleared {len(STALE)} stale v2 entities for device {did}")


def publish_discovery(cli, did, name):
    device = _device_payload(did, name)
    st = f"{BASE}/{did}"

    def cfg(key, extra):
        base = {"unique_id": f"tincan_{did}_{key}", "device": device,
                "availability_topic": AVAIL}
        base.update(extra)
        cli.publish(f"{DISC}/sensor/tincan_{did}/{key}/config", json.dumps(base), retain=True)

    # Live online/offline from /device/status (Bearer-OK). Connectivity binary_sensor.
    base = {"unique_id": f"tincan_{did}_online", "device": device,
            "availability_topic": AVAIL, "name": "Online",
            "state_topic": f"{st}/online", "json_attributes_topic": f"{st}/online_attrs",
            "device_class": "connectivity", "payload_on": "ON", "payload_off": "OFF"}
    cli.publish(f"{DISC}/binary_sensor/tincan_{did}/online/config", json.dumps(base), retain=True)

    # Do Not Disturb (live from /subscriptions -> dnd_enabled; reachable with the Bearer)
    base_dnd = {"unique_id": f"tincan_{did}_dnd", "device": device,
                "availability_topic": AVAIL, "name": "Do Not Disturb",
                "state_topic": f"{st}/dnd", "icon": "mdi:minus-circle",
                "payload_on": "ON", "payload_off": "OFF"}
    cli.publish(f"{DISC}/binary_sensor/tincan_{did}/dnd/config", json.dumps(base_dnd), retain=True)

    # Recent Call — near-live ring signal (ON when the latest CDR started within RING_WINDOW s).
    base_ring = {"unique_id": f"tincan_{did}_recent_call", "device": device,
                 "availability_topic": AVAIL, "name": "Recent Call",
                 "state_topic": f"{st}/recent_call",
                 "json_attributes_topic": f"{st}/recent_call_attrs",
                 "icon": "mdi:phone-ring", "payload_on": "ON", "payload_off": "OFF"}
    cli.publish(f"{DISC}/binary_sensor/tincan_{did}/recent_call/config",
                json.dumps(base_ring), retain=True)

    cfg("phone_number", {
        "name": "Phone Number", "state_topic": f"{st}/phone_number",
        "json_attributes_topic": f"{st}/phone_number_attrs", "icon": "mdi:phone"})
    cfg("provisioning", {
        "name": "Provisioning", "state_topic": f"{st}/provisioning",
        "json_attributes_topic": f"{st}/provisioning_attrs", "icon": "mdi:cellphone-check"})
    cfg("contacts", {
        "name": "Approved Contacts", "state_topic": f"{st}/contacts_count",
        "json_attributes_topic": f"{st}/contacts_attrs",
        "unit_of_measurement": "contacts", "icon": "mdi:contacts"})
    cfg("subscription", {
        "name": "Subscription", "state_topic": f"{st}/subscription",
        "json_attributes_topic": f"{st}/subscription_attrs", "icon": "mdi:card-account-details"})
    cfg("data_source", {
        "name": "Data Source", "state_topic": f"{st}/data_source",
        "json_attributes_topic": f"{st}/data_source_attrs", "icon": "mdi:api",
        "entity_category": "diagnostic"})
    cfg("last_call", {
        "name": "Last Call", "state_topic": f"{st}/last_call",
        "json_attributes_topic": f"{st}/last_call_attrs", "icon": "mdi:phone-log"})
    base_lct = {"unique_id": f"tincan_{did}_last_call_time", "device": device,
                "availability_topic": AVAIL, "name": "Last Call Time",
                "state_topic": f"{st}/last_call_time", "device_class": "timestamp",
                "entity_category": "diagnostic"}
    cli.publish(f"{DISC}/sensor/tincan_{did}/last_call_time/config",
                json.dumps(base_lct), retain=True)


def publish_recent_call(cli, did, last_call, contact_idx=None):
    """Publish Last Call / Last Call Time / Recent Call from one CDR record (or None), enriched
    by matching the other party back to the approved contact book. Shared by the full poll and
    the fast CDR loop so ring detection + caller metadata aren't tied to TC_POLL."""
    st = f"{BASE}/{did}"
    R = dict(retain=True)
    lc = last_call if isinstance(last_call, dict) else {}
    ct = lc.get("call_type", "")
    incoming = (ct == "incoming")
    # the "other party" side of the call
    rid = lc.get("from_routing_id") if incoming else lc.get("to_routing_id")
    onum = lc.get("from_number") if incoming else lc.get("to_number")
    oname = lc.get("from_name") if incoming else lc.get("to_name")
    oavatar = lc.get("from_avatar") if incoming else lc.get("to_avatar")
    # match the CDR's other party back to the contact book (routing id first, then number)
    match = {}
    if contact_idx:
        match = (contact_idx.get(str(rid)) if rid else None) or contact_idx.get(_digits(onum)) or {}
    who = match.get("contact_name") or oname or onum or "Unknown"
    avatar = match.get("contact_avatar") or oavatar
    meta = {
        "call_id": lc.get("call_id"), "direction": ct, "status": lc.get("status"),
        "duration": lc.get("duration"), "start_time": lc.get("start_time"),
        "other_party_name": who, "other_party_number": onum, "routing_id": rid,
        "other_party_is_tincan": lc.get("other_party_is_tincan"),
        "contact_name": match.get("contact_name"), "contact_id": match.get("contact_id"),
        "contact_status": match.get("contact_status"), "speeddial": match.get("speeddial"),
        "group_id": match.get("group_id"), "number_label": match.get("number_label"),
        "entity_picture": avatar}  # HA renders this as the caller's picture on the entity

    if lc:
        cli.publish(f"{st}/last_call", "%s (%s, %s)" % (who, ct, lc.get("status", "")), **R)
        cli.publish(f"{st}/last_call_attrs", json.dumps(meta), **R)
        if lc.get("start_time"):
            cli.publish(f"{st}/last_call_time", lc["start_time"], **R)

    # Recent Call — near-live flag: ON for RING_WINDOW s after the call ENDS (start + duration),
    # so it clears shortly after you hang up instead of lingering from the start time. Missed calls
    # have no recorded answer duration, so for those "end" ~= start (ring length isn't in the data).
    ring_on, secs_since = "OFF", None
    if lc.get("start_time"):
        try:
            t = datetime.fromisoformat(lc["start_time"].replace("Z", "+00:00"))
            end = t + timedelta(seconds=int(lc.get("duration") or 0))
            secs_since = int((datetime.now(timezone.utc) - end).total_seconds())
            if 0 <= secs_since <= RING_WINDOW:
                ring_on = "ON"
        except Exception:
            pass
    cli.publish(f"{st}/recent_call", ring_on, **R)
    cli.publish(f"{st}/recent_call_attrs",
                json.dumps(dict(meta, seconds_since_end=secs_since,
                                window_seconds=RING_WINDOW)), **R)


def publish_state(cli, did, name, sub, dev, contacts, status, last_call=None):
    st = f"{BASE}/{did}"
    R = dict(retain=True)

    # Live online/offline (from /device/status). status is the data dict or None on error.
    if isinstance(status, dict) and status.get("device_status"):
        ds = status.get("device_status")
        cli.publish(f"{st}/online", "ON" if ds == "online" else "OFF", **R)
        cli.publish(f"{st}/online_attrs", json.dumps({
            "device_status": ds, "is_claimed": status.get("is_claimed"),
            "mac_address": status.get("mac_address"),
            "device_type": status.get("device_type"),
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}), **R)
    # on error: leave the retained last value (don't flap to unknown)

    # Phone number / DID (from subscription)
    num = (sub or {}).get("phone_number") or "unknown"
    cli.publish(f"{st}/phone_number", num, **R)
    cli.publish(f"{st}/phone_number_attrs", json.dumps({
        k: (sub or {}).get(k) for k in
        ("phone_number", "provider", "type", "phone_number_id", "emergency_address",
         "internal_id", "subscription_id", "created_at")}), **R)

    # Provisioning / device (from /account/devices)
    cli.publish(f"{st}/provisioning", (dev or {}).get("provisioning_status", "unknown"), **R)
    cli.publish(f"{st}/provisioning_attrs", json.dumps({
        k: (dev or {}).get(k) for k in
        ("device_id", "name", "mac_address", "device_type", "avatar",
         "created_at", "updated_at")}), **R)

    # Contacts (from live search)
    names = sorted({c.get("name", "").strip() for c in contacts if c.get("name")})
    cli.publish(f"{st}/contacts_count", len(names), **R)
    cli.publish(f"{st}/contacts_attrs", json.dumps({"names": names}), **R)

    # Subscription summary (+ DND, now live from /subscriptions)
    sub_state = "active" if (sub and sub.get("subscription_id")) else "unknown"
    cli.publish(f"{st}/subscription", sub_state, **R)
    cli.publish(f"{st}/subscription_attrs", json.dumps({
        k: (sub or {}).get(k) for k in
        ("subscription_id", "type", "provider", "stripe_subscription_id",
         "phone_number", "internal_id", "created_at", "caller_id", "avatar",
         "dnd_enabled", "emergency_enabled", "emergency_status",
         "onboarding_complete", "primary_device_type", "is_v2")}), **R)
    if sub is not None and sub.get("dnd_enabled") is not None:
        cli.publish(f"{st}/dnd", "ON" if sub.get("dnd_enabled") else "OFF", **R)

    # Last Call + Recent Call, enriched with the matched contact-book entry (name/avatar/speeddial).
    # Also refreshed independently by the fast CDR loop; see publish_recent_call.
    publish_recent_call(cli, did, last_call, _contact_index(contacts))

    # Diagnostic: what this bridge can and can't see
    cli.publish(f"{st}/data_source", "live (api.tincan.com)", **R)
    cli.publish(f"{st}/data_source_attrs", json.dumps({
        "note": "Online presence via /device/status and the call log via "
                "/subscription/{id}/cdrs are both live with the WP-JWT Bearer (cdrs route "
                "recovered by runtime capture 2026-10-20). /device/status reports "
                "connectivity only (does not flip during a call); the Last Call sensor "
                "polls the CDR list every TC_POLL seconds.",
        "v2_frozen_since": "2026-06-24"}), **R)


def poll_once(cli):
    # live device list
    std, dj = tc_api.live_get("/account/devices")
    if std != 200:
        print("live /account/devices failed:", std, dj); return False
    devices = dj.get("data", []) if isinstance(dj, dict) else []
    # live subscriptions, keyed by subscription_id
    sts, sj = tc_api.live_get("/subscriptions")
    subs = sj.get("data", []) if (sts == 200 and isinstance(sj, dict)) else []
    sub_by_id = {s.get("subscription_id"): s for s in subs}
    # live call log (CDRs, 1 record) + approved contact book, per subscription
    last_by_sub, contacts_by_sub = {}, {}
    for sid in sub_by_id:
        try:
            cst, cr = tc_api.live_cdrs(sid, limit=1)
            if cst == 200 and isinstance(cr, dict):
                recs = (cr.get("data") or {}).get("records") or []
                last_by_sub[sid] = recs[0] if recs else None
        except Exception as e:
            print("cdrs poll error for %s: %s" % (sid, e))
        try:
            kst, kj = tc_api.live_contacts(sid)   # GET /subscription/{id}/contacts -> data.contacts
            if kst == 200 and isinstance(kj, dict):
                contacts_by_sub[sid] = (kj.get("data") or {}).get("contacts") or []
        except Exception as e:
            print("contacts poll error for %s: %s" % (sid, e))
    _CONTACTS_BY_SUB.clear(); _CONTACTS_BY_SUB.update(contacts_by_sub)
    targets = []

    for dev in devices:
        sub = sub_by_id.get(dev.get("subscription_id")) or (subs[0] if subs else {})
        # keep the HA device id stable on the subscription internal_id (the old numeric id)
        did = (sub.get("internal_id") if sub else None) or dev.get("device_id")
        name = dev.get("name") or f"Tin Can {did}"
        # live online/offline from /device/status (Bearer-OK; needs type + mac params)
        status = None
        mac = dev.get("mac_address")
        dtype = (dev.get("device_type") or "TINCAN").upper()
        if mac:
            q = urllib.parse.urlencode({"device_type": dtype, "mac_address": mac})
            sst, sr = tc_api.live_get("/device/status?" + q)
            if sst == 200 and isinstance(sr, dict):
                status = sr.get("data", sr)
        sid = sub.get("subscription_id") if sub else None
        contacts = contacts_by_sub.get(sid, [])
        last_call = last_by_sub.get(sid)
        _clean_stale(cli, did)
        publish_discovery(cli, did, name)
        publish_state(cli, did, name, sub, dev, contacts, status, last_call)
        if sid:
            targets.append((did, sid))
    _TARGETS[:] = targets

    cli.publish(AVAIL, "online", retain=True)
    _HEARTBEAT["ok"] = time.time()
    ncontacts = sum(len(v) for v in contacts_by_sub.values())
    print("published %d device(s), %d sub(s), %d contact(s) at %s" %
          (len(devices), len(subs), ncontacts, time.strftime("%H:%M:%S")))
    return True


def main():
    cli = _client()
    cli.connect(MQTT_HOST, MQTT_PORT, 60)
    cli.loop_start()
    print("connected to mqtt %s:%d; polling live api every %ds" % (MQTT_HOST, MQTT_PORT, POLL))

    def _watchdog():
        grace = max(120, POLL * 4)
        while True:
            time.sleep(15)
            if time.time() - _HEARTBEAT["ok"] > grace:
                print("watchdog: no successful poll in %ds (likely sleep/dead link); "
                      "exiting for launchd restart" % grace)
                os._exit(1)
    threading.Thread(target=_watchdog, daemon=True).start()

    def _fast_cdr_loop():
        """Poll only the CDR list (1 record/sub) every FAST_POLL s and refresh Last/Recent Call
        (contact-enriched), so ring-detection latency is FAST_POLL, not TC_POLL. One GET per sub."""
        while True:
            time.sleep(FAST_POLL)
            try:
                for did, sid in list(_TARGETS):
                    cst, cr = tc_api.live_cdrs(sid, limit=1)
                    if cst == 200 and isinstance(cr, dict):
                        recs = (cr.get("data") or {}).get("records") or []
                        publish_recent_call(cli, did, recs[0] if recs else None,
                                            _contact_index(_CONTACTS_BY_SUB.get(sid)))
            except Exception as e:
                print("fast cdr loop error:", e)
    threading.Thread(target=_fast_cdr_loop, daemon=True).start()

    while True:
        try:
            poll_once(cli)
        except SystemExit:
            raise
        except Exception as e:
            print("poll error:", e)
            cli.publish(AVAIL, "offline", retain=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
