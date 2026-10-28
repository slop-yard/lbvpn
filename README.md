# mitmvpn

`clients -> Squid LB -> gluetun (ProtonVPN) JP / UK / FR`

Clients (curl, gallery-dl, scrapers, mitmproxy) point at the Squid LB. The LB
picks an exit location three ways, then forwards to the matching gluetun
container's built-in HTTP proxy.

## Layout

```
docker-compose.yml        real stack: 3x gluetun (Proton) + squid LB
compose.test.yml          no-VPN smoke test: 3x stub proxy + squid LB
squid/squid.conf          LB: listeners, selectors, cache_peer per location
squid/Dockerfile          alpine squid; builds demo proxy users
stub/                     fake "VPN" proxy used by compose.test.yml
.env.example              Proton WireGuard keys
```

## 1. Smoke test (no VPN, nothing to configure)

```bash
docker compose -f compose.test.yml up -d --build
```

Each stub answers every plain-HTTP request with `STUB <LOCATION>`, so you can
see exactly which exit the LB chose.

```bash
# pin by credential (username == location)
curl -s -x http://jp:jp@127.0.0.1:3128 http://example.com/     # -> STUB JP
curl -s -x http://fr:fr@127.0.0.1:3128 http://example.com/     # -> STUB FR

# pin by port
curl -s -x http://pool:pool@127.0.0.1:3132 http://example.com/ # -> STUB UK

# pin by header (plain HTTP only)
curl -s -x http://pool:pool@127.0.0.1:3128 \
     -H "X-Exit-Location: fr" http://example.com/              # -> STUB FR

# no selector -> pool policy (sourcehash = sticky per client IP)
for i in 1 2 3; do
  curl -s -x http://pool:pool@127.0.0.1:3128 http://example.com/
done

docker compose -f compose.test.yml down
```

## 2. Real stack (Proton)

1. In the Proton dashboard, generate 3 WireGuard configs (one per location).
   Give each its own tunnel address - the compose file expects
   `10.2/10.3/10.4.0.2/32`.
2. `cp .env.example .env` and paste the 3 private keys.
3. `docker compose up -d --build`

Check the exits:

```bash
docker compose exec vpn-jp sh -c 'wget -qO- https://ipinfo.io/ip'
docker compose exec vpn-uk sh -c 'wget -qO- https://ipinfo.io/ip'
docker compose exec vpn-fr sh -c 'wget -qO- https://ipinfo.io/ip'
```

## 3. Requesting it with curl

The LB is an HTTP proxy on `3128` (pool) plus `3131/3132/3133` (pinned).

```bash
# --- credentials ---
# username selects the location: jp | uk | fr | pool
curl -x http://jp:jp@<lb-host>:3128 https://ipinfo.io/ip

# --- port ---
curl -x http://pool:pool@<lb-host>:3131 https://ipinfo.io/ip   # jp
curl -x http://pool:pool@<lb-host>:3132 https://ipinfo.io/ip   # uk
curl -x http://pool:pool@<lb-host>:3133 https://ipinfo.io/ip   # fr

# --- header on plain HTTP ---
curl -x http://pool:pool@<lb-host>:3128 \
     -H "X-Exit-Location: fr" http://ipinfo.io/ip

# --- header on HTTPS ---
# The header is invisible inside a CONNECT tunnel unless the client puts it
# on the CONNECT request itself. curl can do that with --proxy-header:
curl -x http://pool:pool@<lb-host>:3128 \
     --proxy-header "X-Exit-Location: jp" https://ipinfo.io/ip

# --- pool (no selector) ---
curl -x http://pool:pool@<lb-host>:3128 https://ipinfo.io/ip
```

Handy flags: `-x` = proxy, `-U user:pass` = proxy credentials separately,
`--proxy-header` = header added to the CONNECT request, `--proxy-insecure` =
don't verify the proxy's TLS cert (only relevant for `https://` proxy URLs).

### Other clients

```bash
# gallery-dl
gallery-dl --proxy "http://jp:jp@<lb-host>:3128" <url>

# anything that reads env vars (requests, httpx, curl, wget, searxng...)
export HTTPS_PROXY="http://pool:pool@<lb-host>:3128"
export HTTP_PROXY="http://pool:pool@<lb-host>:3128"
export NO_PROXY="localhost,127.0.0.1,redis"

# mitmproxy as a client of the LB (one fixed upstream per instance)
mitmdump --mode upstream:http://<lb-host>:3128 \
         --upstream-auth jp:jp \
         --set upstream_cert=false
# then point the target at mitmproxy:
curl -x http://127.0.0.1:8080 https://ipinfo.io/ip
```

## Selector summary

Precedence when more than one is present: **header > port > credential > pool**.

| selector          | how                                  | works on HTTPS? |
|-------------------|--------------------------------------|-----------------|
| header            | `-H "X-Exit-Location: jp"`           | only if on CONNECT (`--proxy-header`) |
| port               | `-x http://pool:pool@host:3131`      | yes             |
| credentials       | `-x http://jp:jp@host:3128`          | yes             |
| none              | `pool` user -> sourcehash / round-robin | yes          |

Why the `cache_peer_access` blocks look repetitive: a `pool` credential is
allowed on *every* peer as the fallback, so each peer must explicitly **deny
the other locations' header/port selectors** first. Otherwise `pool` + a
header just falls through to sourcehash and lands on a random exit. ACLs are
first-match-wins, so the ordering in `squid/squid.conf` matters.

Verified against the smoke test (docker compose -f compose.test.yml up -d --build):

```
creds jp        -> STUB JP        pool port 3133  -> STUB FR
creds uk        -> STUB UK        pool hdr fr     -> STUB FR
creds fr        -> STUB FR        pool hdr jp     -> STUB JP
pool port 3131  -> STUB JP        jp creds hdr fr -> STUB FR  (header wins)
pool port 3132  -> STUB UK        pool x5         -> STUB UK  (sticky)
```

## Notes / gotchas

- **Change the demo proxy users.** They're `user:user` and baked into the
  image. For production mount a real `/etc/squid/users`.
- **Firewall the LB.** Don't expose `3128`-`3133` to the internet without auth.
- **Round-robin vs sticky.** `sourcehash` (default here) sticks a calling IP to
  one exit - good for logins/captcha. Replace with `round-robin` on the
  `cache_peer` lines for per-request rotation; also set
  `client_persistent_connections off` or Squid reuses connections and stays
  sticky anyway.
- **Stable exit IP.** gluetun picks a server per location on connect; pin with
  `SERVER_HOSTNAMES=` if you truly need a fixed IP (brittle if that hostname is
  rotated out).
- **Proton caps 10 concurrent connections** per account. 3 locations = 3.
- **HTTP-proxy only** (gluetun's built-in proxy, and Squid). No SOCKS5 unless
  you add `mitmdump --mode socks5` in front.
- **DNS.** Proxied HTTP/HTTPS is resolved VPN-side. Raw DNS lookups (e.g. MX
  records) are not - they'd leave from the VPS IP.
- gluetun uses country **names**, not codes (`United Kingdom`, not `en`).
