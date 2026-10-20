"""Unit test for caller matching, plus live end-to-end tests that boot the integration.

Run:  uv venv -p 3.14 .venv && uv pip install -p .venv/bin/python pytest-homeassistant-custom-component
      .venv/bin/python -m pytest
"""
import os
import pytest
from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.tincan.const import DOMAIN, EVENT_CALL, CONF_REFRESH_TOKEN
from custom_components.tincan.coordinator import CallTracker, enrich_call, build_contact_index, merge_legs

def _env():
    """Load TC_EMAIL / TC_PASSWORD from ./.env or ~/tincan/.env if present."""
    for path in (".env", os.path.expanduser("~/tincan/.env")):
        if not os.path.exists(path):
            continue
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_env()

live = pytest.mark.skipif(
    not (os.environ.get("TC_EMAIL") and os.environ.get("TC_PASSWORD")),
    reason="set TC_EMAIL / TC_PASSWORD (or a .env) to run the live tests",
)


def test_enrich_matches_by_routing_id_then_number():
    contacts = [{"id": "c1", "name": "Alex", "avatar": "http://a/alex.png", "status": "approved",
                 "contact_numbers": [{"dial_plan": "700000", "phone_number": "+15550001111",
                                      "favorite": "speeddial_1", "label": "tincan", "group_id": "g"}]},
                {"id": "c2", "name": "Grandma", "avatar": None, "status": "approved",
                 "contact_numbers": [{"dial_plan": None, "phone_number": "15557654321", "favorite": None}]}]
    idx = build_contact_index(contacts)
    inc = enrich_call({"call_type": "incoming", "from_routing_id": "700000", "from_name": "Alex Doe",
                       "start_time": "2026-10-20T14:34:10.880Z", "duration": 26, "status": "completed"}, idx)
    assert inc.other_party_name == "Alex" and inc.avatar == "http://a/alex.png"
    assert inc.contact["speeddial"] == "speeddial_1"
    assert inc.end.isoformat() == "2026-10-20T14:34:36.880000+00:00"
    out = enrich_call({"call_type": "outgoing", "to_number": "+1 (555) 765-4321", "to_name": "Gma",
                       "start_time": "2026-10-20T14:20:03.115Z", "duration": 0, "status": "missed"}, idx)
    # PSTN "outgoing missed" with a token call_id: the kid dialled out, nobody answered
    assert out.other_party_name == "Grandma" and out.summary == "Grandma (outgoing, missed)"
    assert out.api_call_type == "outgoing" and out.missed is True and out.via_carrier is False
    # same label but a SIP-style call_id: the carrier delivered it, so the phone was rung
    rang = enrich_call({"call_id": "f0603e9bccf2aeb8@0.0.0.0", "call_type": "outgoing", "to_number": "+15557654321",
                        "to_name": "Gma", "status": "missed", "start_time": "2026-10-20T16:57:09.000Z", "duration": 0,
                        "other_party_is_tincan": False}, idx)
    assert rang.direction == "incoming" and rang.via_carrier and rang.other_party_name == "Grandma"
    assert rang.attributes()["direction_source"] == "call_id" and rang.summary == "Grandma (incoming, missed)"
    # PSTN "incoming" with a UUID id (parent app -> kid) stays incoming
    pstn_in = enrich_call({"call_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "call_type": "incoming",
                           "from_number": "+15557654321", "status": "completed",
                           "start_time": "2026-10-20T14:20:03.115Z", "duration": 9, "other_party_is_tincan": False}, idx)
    assert pstn_in.direction == "incoming" and pstn_in.other_party_name == "Grandma"
    assert pstn_in.attributes()["direction_source"] == "call_type"
    # "missed" call_type is always an unanswered inbound call, caller under from_
    tc_missed = enrich_call({"call_type": "missed", "from_routing_id": "700000", "status": "voicemail",
                             "start_time": "2026-10-20T14:20:03.115Z", "duration": 0, "other_party_is_tincan": True}, idx)
    assert tc_missed.direction == "incoming" and tc_missed.missed and tc_missed.other_party_name == "Alex"
    # Tin Can outgoing stays outgoing
    tc_out = enrich_call({"call_type": "outgoing", "to_routing_id": "700000", "status": "completed",
                          "start_time": "2026-10-20T14:20:03.115Z", "duration": 5, "other_party_is_tincan": True}, idx)
    assert tc_out.direction == "outgoing" and tc_out.other_party_name == "Alex"
    unk = enrich_call({"call_type": "incoming", "from_number": "+15559999999", "start_time": None}, idx)
    assert unk.other_party_name == "+15559999999" and unk.end is None



# ---- call lifecycle: replays of the record sequences seen in live test calls
T0 = dt_util.parse_datetime("2026-10-20T18:05:32.000Z")


def _rec(call_id, status, duration, start=T0):
    return {"call_id": call_id, "call_type": "outgoing", "status": status, "duration": duration,
            "start_time": start.isoformat().replace("+00:00", "Z"), "to_name": "Dad",
            "to_number": "+15550001111", "other_party_is_tincan": False}


def _replay(steps, prior=None):
    """Feed (seconds_after_T0, records) polls to a tracker -> [(phase, missed, fired), ...]."""
    tracker, out = CallTracker(), []
    if prior is not None:  # an older, finished call is newest when tracking starts
        tracker.update(prior, {}, T0 - timedelta(minutes=5))
    for secs, records in steps:
        call, fired = tracker.update(records, {}, T0 + timedelta(seconds=secs))
        out.append((call.phase, call.missed, fired))
    return tracker, out


OLD = [_rec("old@0.0.0.0", "completed", 13, T0 - timedelta(hours=1))]


def test_answered_call_is_never_reported_missed():
    # observed: missed/0 (ringing) -> completed/0 at +15s (picked up) -> completed/13 at +28s
    _, out = _replay([(2, [_rec("a@h", "missed", 0)] + OLD),
                      (10, [_rec("a@h", "missed", 0)] + OLD),
                      (15, [_rec("a@h", "completed", 0)] + OLD),
                      (28, [_rec("a@h", "completed", 13)] + OLD),
                      (36, [_rec("a@h", "completed", 13)] + OLD)], prior=OLD)
    assert out == [("ringing", False, False), ("ringing", False, False),
                   ("in_call", False, False), ("ended", False, True), ("ended", False, False)]


def test_unanswered_call_is_missed_only_after_it_rings_out():
    # observed: missed/0 -> completed/0 at +48s (ringing out) -> missed/0 at +64s (final)
    _, out = _replay([(3, [_rec("m@h", "missed", 0)] + OLD),
                      (40, [_rec("m@h", "missed", 0)] + OLD),
                      (48, [_rec("m@h", "completed", 0)] + OLD),
                      (64, [_rec("m@h", "missed", 0)] + OLD),
                      (72, [_rec("m@h", "missed", 0)] + OLD)], prior=OLD)
    assert out == [("ringing", False, False), ("ringing", False, False), ("in_call", False, False),
                   ("missed", True, True), ("missed", True, False)]


def test_missed_without_seeing_ring_out_settles_after_timeout():
    # e.g. the caller hangs up early, or a poll missed the ring-out flip
    # observed: hanging up while it rings leaves the record untouched at missed/0
    _, out = _replay([(5, [_rec("m@h", "missed", 0)]), (50, [_rec("m@h", "missed", 0)]),
                      (56, [_rec("m@h", "missed", 0)])], prior=OLD)
    assert out == [("ringing", False, False), ("ringing", False, False), ("missed", True, True)]


def test_calls_already_over_at_startup_do_not_fire():
    tracker = CallTracker()
    call, fired = tracker.update(OLD, {}, T0)
    assert call.phase == "ended" and not fired
    # later polls must not mistake it for a call that just ended (no event, end unchanged)
    again, fired = tracker.update(OLD, {}, T0 + timedelta(seconds=8))
    assert not fired and again.end == call.end == T0 - timedelta(hours=1) + timedelta(seconds=13)
    # but a call that is still ringing at startup fires when it settles
    tracker = CallTracker()
    assert tracker.update([_rec("r@h", "missed", 0)], {}, T0 + timedelta(seconds=5))[1] is False
    assert tracker.update([_rec("r@h", "completed", 4)], {}, T0 + timedelta(seconds=30))[1] is True


def test_call_legs_merge_into_one_call():
    # one call logged twice (token + UUID ids, same start second), statuses drifting apart
    legs = [_rec("gZ1lUVLphm7K9c", "missed", 0), _rec("313aaf13-d477-4f", "completed", 9)]
    key, merged = merge_legs(legs + OLD)
    assert key == "2026-10-20T18:05:32" and merged["status"] == "completed" and merged["duration"] == 9
    assert merged["call_id"] == "313aaf13-d477-4f"  # stable: sorted, not whichever leg sorts first
    _, out = _replay([(30, legs + OLD), (38, list(reversed(legs)) + OLD)], prior=OLD)
    assert out == [("ended", False, True), ("ended", False, False)]  # one event, not two


def test_end_time_is_when_the_call_was_seen_ending():
    tracker = CallTracker()
    tracker.update(OLD, {}, T0)
    tracker.update([_rec("a@h", "completed", 0)], {}, T0 + timedelta(seconds=15))
    call, _ = tracker.update([_rec("a@h", "completed", 13)], {}, T0 + timedelta(seconds=28))
    # duration counts talk time only, so start + duration (T0+13s) would be too early
    assert call.end == T0 + timedelta(seconds=28)
    # a call that is still ringing has no end yet
    ringing, _ = CallTracker().update([_rec("r@h", "missed", 0)], {}, T0 + timedelta(seconds=5))
    assert ringing.end is None

@pytest.fixture
def threaded_resolver():
    """Let the HA client session reach the real internet inside the test harness."""
    import aiohttp

    class _Resolver(aiohttp.ThreadedResolver):
        async def real_close(self):  # HA's own resolver subclass has this
            await self.close()

    import pytest_socket
    for fn in ("_remove_restrictions", "enable_socket"):
        if hasattr(pytest_socket, fn):
            getattr(pytest_socket, fn)()
    with patch("homeassistant.helpers.aiohttp_client._async_make_resolver", side_effect=lambda hass: _Resolver()), \
         patch("socket.getaddrinfo", __import__("_socket").getaddrinfo):
        yield

@live
@pytest.mark.enable_socket
async def test_live_setup(hass: HomeAssistant, socket_enabled, threaded_resolver):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=os.environ["TC_EMAIL"].lower(), title="live",
                            data={CONF_EMAIL: os.environ["TC_EMAIL"], CONF_PASSWORD: os.environ["TC_PASSWORD"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded", entry.state
    assert entry.data.get(CONF_REFRESH_TOKEN), "refresh token should be persisted into entry.data"

    states = {s.entity_id: s for s in hass.states.async_all() if s.entity_id.split(".")[1].startswith(("tin_can", "robin", "tincan")) or s.domain in ("sensor", "binary_sensor")}
    print("\n--- entities ---")
    for eid, st in sorted(states.items()):
        attrs = {k: v for k, v in st.attributes.items() if k in ("device_class", "entity_picture", "seconds_since_end", "other_party_name", "direction", "status", "names", "speeddials", "device_status")}
        print(f"{eid:60s} {st.state!s:40.40s} {attrs}")
    ids = set(states)
    for suffix in ("online", "do_not_disturb", "recent_call"):
        assert any(e.startswith("binary_sensor.") and e.endswith(suffix) for e in ids), suffix
    for suffix in ("phone_number", "approved_contacts", "subscription", "provisioning", "last_call", "last_call_time"):
        assert any(e.startswith("sensor.") and e.endswith(suffix) for e in ids), suffix
    online = next(s for e, s in states.items() if e.endswith("_online"))
    assert online.state in ("on", "off")
    lc = next(s for e, s in states.items() if e.endswith("_last_call"))
    assert lc.state not in ("unknown", "unavailable")
    lct = next(s for e, s in states.items() if e.endswith("_last_call_time"))
    assert lct.attributes["device_class"] == "timestamp" and "T" in lct.state

    # device registry: one device per line
    from homeassistant.helpers import device_registry as dr
    devs = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    print("devices:", [(d.name, d.manufacturer, d.model) for d in devs])
    assert len(devs) == 1

    # drive both coordinators through another cycle
    events = []
    hass.bus.async_listen(EVENT_CALL, lambda e: events.append(e))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert entry.runtime_data.account.last_update_success and entry.runtime_data.calls.last_update_success
    print("events fired on refresh (expect 0, same call):", len(events))

    # options reload path
    hass.config_entries.async_update_entry(entry, options={"poll_interval": 60, "call_poll_interval": 10, "ring_window": 45})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "loaded"
    assert entry.runtime_data.calls.ring_window.total_seconds() == 45
    rc = next(s for s in hass.states.async_all("binary_sensor") if s.entity_id.endswith("_recent_call"))
    assert rc.attributes["window_seconds"] == 45

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.value == "not_loaded"


@live
@pytest.mark.enable_socket
async def test_config_flow_bad_password(hass: HomeAssistant, socket_enabled, threaded_resolver):
    from homeassistant import config_entries
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == "form" and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_EMAIL: os.environ["TC_EMAIL"], CONF_PASSWORD: "nope"})
    assert result["type"] == "form" and result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_EMAIL: os.environ["TC_EMAIL"], CONF_PASSWORD: os.environ["TC_PASSWORD"]})
    assert result["type"] == "create_entry", result
    assert result["data"][CONF_REFRESH_TOKEN]
    assert result["title"] == os.environ["TC_EMAIL"].lower()
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state.value == "loaded"
    # options flow shows with defaults
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form" and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"poll_interval": 20, "call_poll_interval": 5, "ring_window": 30})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["call_poll_interval"] == 5 and isinstance(entry.options["call_poll_interval"], int)
    # duplicate account aborts
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_EMAIL: os.environ["TC_EMAIL"].upper(), CONF_PASSWORD: "x"})
    assert result["type"] == "abort" and result["reason"] == "already_configured"
