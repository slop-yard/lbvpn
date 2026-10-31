# lbvpn

Scan a folder of WireGuard configs and generate a self-contained Docker
Compose bundle: **one exit per `.conf` file, behind one nginx load balancer**,
plus optional consumer modules.

```
  consumers            LB                      exits (.conf files)
  ┌──────────┐
  │ searxng  │──────┐                    ┌────► vpn-jp275   ◄─ jp275.conf
  ├──────────┤      │   ┌────────────┐   ├─────► vpn-fr480   ◄─ fr480.conf
  │ mitmproxy│──────┼──►│  lb :8890  │───┤
  ├──────────┤      │   │  :3131+    │   └─────► ...
  │ debug    │──────┘   └────────────┘
  └──────────┘          :8890 pool · :3131+ one exit each
```

## Usage

```sh
python generate.py --scan ./my-proton-confs --with-mitmproxy --with-debug ./vpn
```

| flag | |
|---|---|
| `--scan ./dir` | **required**; every `*.conf` in there becomes one exit |
| `--lb-mode` | pool policy: `rr` (default) \| `<region id>` \| `<host:port>` |
| `--lb-port` | pool listener (default `8890`) |
| `--region-port-base` | first per-exit listener (default `3131`, +1 each) |
| `--bind` | host address to publish on (default `127.0.0.1`) |
| `--with-<module>` | bundle a module (see below) |
| `--list-mods` | list modules |

There is no env file input. The credentials live in the `.conf` files, so the
generated `.env.example` only tunes the pool and the modules.

## Labelling an exit

For each `.conf`, lbvpn tries to find a Proton-style server name in the file -
anywhere, including a comment you add yourself:

```
# JP#275
[Interface]
PrivateKey = ...
```

Found → the exit id is `jp275`. Not found → the file name is used
(`my-tokyo-vps.conf` → `mytokyovps`). The id drives the
service name (`vpn-<id>`), the config copy (`configs/wireguard/<id>.conf`) and
the per-exit port, so keep them unique.

Two files resolving to the same id is an error, not a silent merge.

## Output

```
vpn/
  compose.yaml
  .env.example              copy to .env (only tuning - no credentials in it)
  README.md
  configs/lb-entrypoint.sh
  configs/wireguard/<id>.conf   your confs, copied and normalized
  configs/searxng.yaml      (with searxng)
  configs/debug.py          (with debug)
  compose.stub.yaml         (with stub)
  stub/                     (with stub)
```

## Run it

```sh
cd vpn
docker compose up -d

curl -x http://127.0.0.1:8890 https://ipinfo.io/ip   # the pool
curl -x http://127.0.0.1:3131 https://ipinfo.io/ip   # exactly one exit
```

Per-exit ports are `--region-port-base + index` in the order the files were
scanned (alphabetical). The pool balances per **TCP connection**, not per
request; per-exit ports are exact regardless.

`LB_MODE` in `.env` changes the pool policy (`rr` | `<id>` | `host:port`) —
then `docker compose up -d lb`.

## Modules

| module | requires | what it adds |
|---|---|---|
| `--with-searxng` | – | SearXNG, engine requests go to the LB pool (`:8080`) |
| `--with-mitmproxy` | – | mitmweb, upstream = LB pool (`:8181` proxy, `:8081` web UI) |
| `--with-debug` | mitmproxy | tiny server reporting the egress IP per exit (`:9000`) |
| `--with-mxrelay` | mitmproxy | fetch-as-a-service: `?url=<target>` relays method/headers/body through the chain (`:9001`, `X-MX-RELAY-AUTH` header auth) |
| `--with-stub` | – | `compose.stub.yaml`, test the LB with no VPN keys |

Modules are consumers of the LB, never in each other's path. Registering a new
one is a template plus a `MODULES` entry in `generate.py`.

## Gotchas

- **Each `.conf` is one Proton key = one of your account's concurrent
  connections** (Plus ~10, free 1).
- **A `.conf` with several `[Peer]` blocks**: only the first is used.
- **A missing `configs/wireguard/<id>.conf`** makes Docker create a *directory*
  at that path and gluetun fails confusingly. Regenerate after adding files.
- **The LB needs `zone vpns 64k;`** — without shared memory nginx's round-robin
  counter is per worker and short-lived connections all hit the first server.
- **The pool balances per TCP connection**, so a keep-alive client holds one
  exit. (For SearXNG alone, listing several proxies in `outgoing.proxies`
  rotates per request and needs no LB at all.)
- **`xx#nn` exits use `VPN_SERVICE_PROVIDER: custom`** and mount the conf as
  `/gluetun/wireguard/wg0.conf`. The key, address and endpoint all come from
  the file, so `.env` carries no credentials - but you lose NAT-PMP port
  forwarding and gluetun's server list for those regions, and the config dies
  if Proton retires the server.
- **Check egress over HTTPS, never plain HTTP.** `api.ipify.org` honours a
  client-supplied `X-Forwarded-For`, which the proxy chain sets to the
  immediate client's private address - so a plain-HTTP lookup reports the
  *proxy*, not the exit. `icanhazip.com` and `ifconfig.me` are unaffected, and
  nothing can inject headers into a tunneled request, so HTTPS is always
  correct. This is why `--with-debug` uses HTTPS with verification off.
- **mitmproxy replaces the TLS fingerprint.** It terminates and re-originates
  TLS, so the target sees mitmproxy's fingerprint, not the client's - which
  defeats impersonation (`curl_cffi`, browser TLS). Keep clients that rely on
  that pointed at the LB directly.
- **mitmproxy's `socks5` mode ignores the upstream proxy.** Combining
  `--mode upstream:...` with `--mode socks5` makes the SOCKS listener go
  *direct*, bypassing the VPN. Don't add a SOCKS listener to a VPN chain
  without checking where it egresses.
- **`--with-mxrelay` is an open fetch relay for anyone holding the token** -
  it fetches arbitrary URLs through the chain (SSRF surface included), so keep
  it bound to `127.0.0.1`/the tailnet and rotate the token if it ever leaks.
- **mitmweb requires a password** when bound to a non-loopback address; the
  generated one is in `.env.example` as `MITM_WEB_PASSWORD`.
- **Proton exit IPs get CAPTCHA'd** by Google/Bing; expect some engines to fail.

## Project layout

```
generate.py     generator (stdlib only)
templates/      compose.yaml, region-wireguard.yaml, lb-entrypoint.sh,
                searxng-*.yaml, mitmproxy-service.yaml, debug*.yaml,
                env.example, README.md, compose.stub.yaml
stub/           stub HTTP proxy, copied with --with-stub
```

`templates/` is the single source of truth; nothing is generated by hand.
