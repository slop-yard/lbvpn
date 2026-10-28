@@PROJECT@@
@@UNDERLINE@@

@@REGION_COUNT@@ Proton exit(s) @@REGIONS@@ behind one load balancer@@SEARXNG_NOTE@@.

Files
  compose.yaml                the stack
  .env.example                copy to .env, then fill in the WireGuard keys
  configs/lb-entrypoint.sh    LB init script (reads LB_MODE / VPN_HOSTS)
@@SEARXNG_FILES@@@@STUB_FILES@@
Quick start
  cp .env.example .env
  # edit .env: set <REGION>_WG_KEY and <REGION>_WG_ADDRESS for every region
  docker compose up -d

Check it
  curl -x http://127.0.0.1:@@LB_PORT@@ https://ipinfo.io/ip
@@SEARXNG_CHECK@@
Load balancer
  LB_MODE=rr               round-robin over every region (default)
  LB_MODE=@@FIRST_REGION@@               pin everything to one region
  LB_MODE=host:port        pin to an explicit upstream
  Set it in .env, or edit lb.environment.LB_MODE in compose.yaml.

  The LB balances per TCP connection, not per request.

Regions
@@REGION_LINES@@

  <REGION>_COUNTRY overrides the country name used for the server filter.
  <REGION>_CITY    optionally narrows to a city.
