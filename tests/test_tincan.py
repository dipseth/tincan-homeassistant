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
from custom_components.tincan.coordinator import enrich_call, build_contact_index

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
