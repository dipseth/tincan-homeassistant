#!/usr/bin/env python3
"""tc_api.py — Tin Can cloud read client (local-only).

ARCHITECTURE (reverse-engineered from the app.tincan APK + live probing):
  * Auth is WordPress "JWT Authentication for WP REST API" at archie.tincan.kids.
      POST https://archie.tincan.kids/wp-json/jwt-auth/v1/token  {username: <email>, password}
        -> data.token (HS256 JWT, iss=archie.tincan.kids, ~60 min) + data.refresh_token
      POST https://archie.tincan.kids/wp-json/jwt-auth/v1/token/refresh  {refresh_token}
        -> data.token (renew without the password)
  * The JWT is the Bearer for Tin Can's REST API at https://api.tincan.com/v2 (AWS API GW).
      GET /v2/devices            -> the phone(s): device_status online/offline, dnd, name, number
      GET /v2/devices/{id}       -> one device, full detail
      GET /v2/contacts           -> approved contacts
    (Live call state / call history appear to live in Firestore, not this REST API — see probe.)

CREDENTIALS (read from project .env or ~/tincan/.tc_creds; never printed):
  TC_EMAIL, TC_PASSWORD        your Tin Can parent login
  (optional) TC_API_BASE, TC_AUTH_BASE
Token cache: ~/tincan/.tc_token (chmod 600), holds the JWT + refresh token.

COMMANDS:
  login            password login; cache JWT + refresh token
  refresh          renew the JWT using the cached refresh token
  token            show cached token status (redacted)
  devices          GET /v2/devices
  device [id]      GET /v2/devices/{id}  (defaults to the first device)
  contacts         GET /v2/contacts
  cdrs [limit]     probe for call detail records (caller id + call status/history)
  raw <path>       GET any path under /v2, e.g.  raw /devices
  --- live (current) API at api.tincan.com; /v2 above is FROZEN since 2026-06-24 ---
  account          GET /account                 (live account; firestore_uid array)
  ldevices         GET /account/devices         (live device list: uuid, name, mac)
  subs             GET /subscriptions           (live: phone number / DID, plan)
  lcontacts [q]    GET /contacts?q=<q>          (live contact search; default q=space)
  status           GET /device/status           (LIVE device presence; Bearer-OK)
  watch [secs]     fast-poll /device/status, print on change (default 3s) — use during a
                   real call to see if device_status flips (online/idle/busy/...)
  lraw <path>      GET any live path (403 signature errors do NOT establish IAM or route existence)
  probe            hit candidate routes and print status + preview (route discovery)
"""
import json, os, sys, time, base64, urllib.request, urllib.error, urllib.parse

HOME       = os.path.expanduser("~/tincan")
PROJ_DIR   = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(HOME, ".tc_creds")
TOKEN_FILE = os.path.join(HOME, ".tc_token")
UA         = "okhttp/4.12.0"


def _load_env_files():
    for path in (os.path.join(PROJ_DIR, ".env"), CREDS_FILE):
        if not os.path.exists(path):
            continue
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_files()

API_BASE  = os.environ.get("TC_API_BASE", "https://api.tincan.com/v2")   # FROZEN since 2026-06-24
LIVE_BASE = os.environ.get("TC_LIVE_BASE", "https://api.tincan.com")     # current prod (Bearer on some routes)
AUTH_BASE = os.environ.get("TC_AUTH_BASE", "https://archie.tincan.kids/wp-json/jwt-auth/v1")


def _req(method, url, body=None, bearer=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if bearer:
        h["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {"_raw": raw[:2000]}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"_raw": "<non-json error>"}


def _jwt_exp(token):
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp", 0))
    except Exception:
        return 0


def _save(d):
    os.makedirs(HOME, exist_ok=True)
    cur = {}
    if os.path.exists(TOKEN_FILE):
        try:
            cur = json.load(open(TOKEN_FILE))
        except Exception:
            cur = {}
    cur.update(d)
    json.dump(cur, open(TOKEN_FILE, "w"))
    os.chmod(TOKEN_FILE, 0o600)


def _load():
    if not os.path.exists(TOKEN_FILE):
        sys.exit("No token cache. Run:  python3 tc_api.py login")
    return json.load(open(TOKEN_FILE))


def _creds():
    e, p = os.environ.get("TC_EMAIL"), os.environ.get("TC_PASSWORD")
    if not e or not p:
        sys.exit("TC_EMAIL / TC_PASSWORD not set (add to %s/.env)" % PROJ_DIR)
    return e, p


def cmd_login():
    email, pw = _creds()
    st, r = _req("POST", AUTH_BASE + "/token", {"username": email, "password": pw})
    data = r.get("data", {}) if isinstance(r, dict) else {}
    token = data.get("token")
    if st != 200 or not token:
        sys.exit("login failed [%s]: %s" % (st, r.get("message", r)))
    _save({"token": token,
           "refresh_token": data.get("refresh_token") or data.get("refreshToken"),
           "exp": _jwt_exp(token)})
    print("login ok. token ~%d min; refresh token %s" %
          ((_jwt_exp(token) - time.time()) / 60,
           "saved" if (data.get("refresh_token") or data.get("refreshToken")) else "ABSENT"))


def cmd_refresh():
    t = _load()
    rt = t.get("refresh_token")
    if not rt:
        sys.exit("no refresh token cached; run login")
    st, r = _req("POST", AUTH_BASE + "/token/refresh", {"refresh_token": rt})
    data = r.get("data", {}) if isinstance(r, dict) else {}
    token = data.get("token")
    if st != 200 or not token:
        print("refresh failed [%s]: %s — falling back to login" % (st, r.get("message", r)))
        return cmd_login()
    _save({"token": token, "exp": _jwt_exp(token),
           "refresh_token": data.get("refresh_token") or rt})
    print("refreshed. token ~%d min" % ((_jwt_exp(token) - time.time()) / 60))


def token():
    t = _load()
    if t.get("token") and t.get("exp", 0) > time.time() + 60:
        return t["token"]
    # try refresh, else require login
    rt = t.get("refresh_token")
    if rt:
        st, r = _req("POST", AUTH_BASE + "/token/refresh", {"refresh_token": rt})
        data = r.get("data", {}) if isinstance(r, dict) else {}
        if st == 200 and data.get("token"):
            _save({"token": data["token"], "exp": _jwt_exp(data["token"]),
                   "refresh_token": data.get("refresh_token") or rt})
            return data["token"]
    # last resort: password login if creds present
    if os.environ.get("TC_EMAIL") and os.environ.get("TC_PASSWORD"):
        cmd_login()
        return _load()["token"]
    sys.exit("token expired and refresh failed; run:  python3 tc_api.py login")


def cmd_token():
    t = _load()
    print("token ~%d min left; refresh_token: %s" %
          ((t.get("exp", 0) - time.time()) / 60, "yes" if t.get("refresh_token") else "no"))


def api_get(path):
    if not path.startswith("/"):
        path = "/" + path
    return _req("GET", API_BASE + path, bearer=token())


def live_get(path):
    """GET against the current API (api.tincan.com, empty base).

    Bearer works on account/device/contact/subscription reads and correctly
    parameterized /device/status. The current call-history contract is unknown.
    A random nonexistent-path control returned the same signature error as /calls;
    that error establishes neither route existence nor an IAM requirement.
    See captures/gateway-control-findings-20261020.md.
    """
    if not path.startswith("/"):
        path = "/" + path
    return _req("GET", LIVE_BASE + path, bearer=token())


def _show_live(path):
    st, data = live_get(path)
    print("[%s] GET %s%s" % (st, LIVE_BASE, path))
    print(json.dumps(data, indent=2)[:4000])


def _first_device_id():
    st, r = api_get("/devices")
    if st == 200 and r.get("data"):
        return r["data"][0]["device_id"]
    return None


def _show(path):
    st, data = api_get(path)
    print("[%s] GET %s" % (st, path))
    print(json.dumps(data, indent=2)[:4000])


def _live_device():
    """Return (mac_address, DEVICE_TYPE) for the account's first live device.
    /device/status requires both as query params; device_type must be upper-case
    (the enum is HT801|TINCAN, but /account/devices reports it lower-case)."""
    st, r = live_get("/account/devices")
    devs = r.get("data") if isinstance(r, dict) else None
    if st != 200 or not devs:
        sys.exit("could not read /account/devices [%s]: %s" % (st, r))
    d = devs[0]
    mac = d.get("mac_address")
    dtype = (d.get("device_type") or "TINCAN").upper()
    if not mac:
        sys.exit("device has no mac_address: %s" % json.dumps(d))
    return mac, dtype


def _device_status():
    """GET /device/status for the first device. Returns (http_status, data_dict)."""
    mac, dtype = _live_device()
    q = urllib.parse.urlencode({"device_type": dtype, "mac_address": mac})
    st, r = live_get("/device/status?" + q)
    data = r.get("data", r) if isinstance(r, dict) else r
    return st, data


def cmd_status():
    st, data = _device_status()
    print("[%s] GET /device/status" % st)
    print(json.dumps(data, indent=2)[:2000])


def cmd_watch(secs=3):
    """Fast-poll /device/status, printing only when device_status changes (plus a
    heartbeat line each poll). Ctrl-C to stop. Use during a real call."""
    print("watching /device/status every %ss — place/receive a call now (Ctrl-C to stop)" % secs)
    last = object()
    while True:
        try:
            st, data = _device_status()
            cur = data.get("device_status") if isinstance(data, dict) else None
            stamp = time.strftime("%H:%M:%S")
            if cur != last:
                print("%s  *** device_status = %r  [%s]  %s" %
                      (stamp, cur, st, json.dumps(data) if isinstance(data, dict) else data))
                last = cur
            else:
                print("%s  ... %r" % (stamp, cur))
            time.sleep(secs)
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        except Exception as e:
            print("%s  poll error: %s" % (time.strftime("%H:%M:%S"), e))
            time.sleep(secs)


def cmd_probe():
    tok = token()
    did = _first_device_id() or 0
    paths = ["/devices", "/devices/%s" % did, "/contacts",
             "/calls", "/devices/%s/calls" % did, "/call-logs", "/voicemails",
             "/me", "/user", "/account", "/devices/%s/status" % did]
    for p in paths:
        st, r = _req("GET", API_BASE + p, bearer=tok)
        print("%-4s %-26s %s" % (st, p, json.dumps(r)[:120]))


def _candidate_cdr_urls(limit):
    """CDR endpoint base is unconfirmed from static analysis. The app's call-log
    client hits /cdrs on the user-data API; prod config points that at api.tincan.com
    with an empty base path, but /v2 is what devices/contacts actually use, and
    environment.json also carries a /v3 gateway. Try the likely combos, read-only."""
    q = urllib.parse.urlencode({"limit": limit, "page": 1})
    bases = [
        API_BASE,                    # https://api.tincan.com/v2 (same base as devices)
        "https://api.tincan.com",    # empty-base USER_DATA_API_URL from prod .env
        "https://api.tincan.com/v3",
    ]
    urls = []
    env_url = os.environ.get("TC_CDRS_URL")   # explicit override wins
    if env_url:
        urls.append(env_url if "?" in env_url else env_url + "?" + q)
    for b in bases:
        urls.append("%s/cdrs?%s" % (b, q))
    did = _first_device_id()
    if did:
        for b in bases:
            urls.append("%s/devices/%s/cdrs?%s" % (b, did, q))
    return urls, did


def _records_from(body):
    """Pull the record list out of whatever wrapper the server uses."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "results", "items", "records", "cdrs"):
            v = body.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("data", "results", "items", "records", "cdrs"):
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return None


def _fmt_record(r):
    if not isinstance(r, dict):
        return "  " + json.dumps(r)[:100]
    g = lambda *ks: next((str(r[k]) for k in ks if r.get(k) not in (None, "")), "")
    return "  %-10s  %-20s %-9s ->  %-16s %-9s  %s" % (
        g("status", "call_status", "callStatus"),
        g("from_device_name", "display_name", "contact_name", "name"),
        g("from_number", "fromNumber", "caller_id", "callerId"),
        g("to_device_name"),
        g("to_number", "toNumber", "did_number"),
        g("start_time", "startTime", "started_at", "created_at"),
    )


def subscription_ids():
    """Live subscription ids for this account (GET /subscriptions -> data[].subscription_id)."""
    st, r = live_get("/subscriptions")
    if st == 200 and isinstance(r, dict):
        return [s["subscription_id"] for s in r.get("data", []) if s.get("subscription_id")]
    return []


def live_cdrs(sub_id, limit=50, page=1):
    """The real call-log route, confirmed by runtime capture (2026-10-20):
      GET /subscription/{sub_id}/cdrs?page&limit&sort_by=start_time&order=desc
    Returns (http_status, body). body["data"]["records"] is the call list (newest first)."""
    q = urllib.parse.urlencode({"page": page, "limit": limit,
                                "sort_by": "start_time", "order": "desc"})
    return live_get("/subscription/%s/cdrs?%s" % (sub_id, q))


def live_contacts(sub_id):
    """Approved contact book for a subscription (the kid's REAL contacts, NOT the global
    /contacts?q= directory of all Tin Can users): GET /subscription/{id}/contacts -> data.contacts[]."""
    return live_get("/subscription/%s/contacts" % sub_id)


def _fmt_cdr(r):
    """One line for a CDR record. Direction decides which party is 'the other end'."""
    ct = r.get("call_type", "?")
    if ct == "incoming":
        who = r.get("from_name") or r.get("from_number") or r.get("from_routing_id") or "?"
    else:
        who = r.get("to_name") or r.get("to_number") or r.get("to_routing_id") or "?"
    tc = "tincan" if r.get("other_party_is_tincan") else "pstn"
    return "  %-24s %-9s %-10s %4ss  %-6s %s" % (
        r.get("start_time", ""), ct, r.get("status", ""), r.get("duration", 0), tc, who)


def cmd_cdrs(limit=20):
    subs = subscription_ids()
    if not subs:
        sys.exit("no subscriptions found (need a valid Bearer; run: python3 tc_api.py login)")
    sub = subs[0]
    st, r = live_cdrs(sub, limit=limit)
    if st != 200 or not isinstance(r, dict):
        sys.exit("cdrs failed [%s]: %s" % (st, json.dumps(r)[:300]))
    data = r.get("data") or {}
    recs = data.get("records", [])
    print("subscription %s — showing %d of %s records (page %s/%s)" %
          (sub, min(limit, len(recs)), data.get("total"), data.get("page"), data.get("totalPages")))
    print("  %-24s %-9s %-10s %5s  %-6s %s" %
          ("START (UTC)", "DIR", "STATUS", "DUR", "NET", "OTHER PARTY"))
    for rec in recs[:limit]:
        print(_fmt_cdr(rec))
    if recs:
        print("\nfirst raw record:")
        print(json.dumps(recs[0], indent=2)[:1200])


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return
    c = a[0]
    if c == "login":      cmd_login()
    elif c == "refresh":  cmd_refresh()
    elif c == "token":    cmd_token()
    elif c == "probe":    cmd_probe()
    elif c == "devices":  _show("/devices")
    elif c == "device":   _show("/devices/%s" % (a[1] if len(a) > 1 else _first_device_id()))
    elif c == "contacts": _show("/contacts")
    elif c == "cdrs":     cmd_cdrs(int(a[1]) if len(a) > 1 else 20)
    elif c == "raw" and len(a) > 1: _show(a[1])
    # --- live (current) API, api.tincan.com ---
    elif c == "account":  _show_live("/account")
    elif c == "ldevices": _show_live("/account/devices")
    elif c == "subs":     _show_live("/subscriptions")
    elif c == "lcontacts":_show_live("/contacts?q=%s&limit=200" %
                                     urllib.parse.quote(a[1] if len(a) > 1 else " "))
    elif c == "status":   cmd_status()
    elif c == "watch":    cmd_watch(float(a[1]) if len(a) > 1 else 3)
    elif c == "lraw" and len(a) > 1: _show_live(a[1])
    else: sys.exit("unknown command: %s" % c)


if __name__ == "__main__":
    main()
