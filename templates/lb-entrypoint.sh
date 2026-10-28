#!/bin/sh
set -eu

# lbvpn load balancer. Builds the nginx config at container start.
#
#   POOL_PORT          the round-robin (or pinned) listener
#   LB_MODE            rr | <region> | <host:port>   policy of the pool listener
#   VPN_HOSTS          pool members, e.g. "vpn-fr:8888 vpn-en:8888"
#   REGION_ENDPOINTS   exact per-region listeners, e.g. "3131=vpn-fr:8888 3132=vpn-en:8888"
: "${LB_MODE:=rr}"
: "${VPN_HOSTS:=}"
: "${REGION_ENDPOINTS:=}"
: "${POOL_PORT:=8890}"

case "$LB_MODE" in
  rr|round-robin)
    if [ -z "$VPN_HOSTS" ]; then
      echo "LB_MODE=$LB_MODE needs VPN_HOSTS" >&2
      exit 1
    fi
    pool=""
    for h in $VPN_HOSTS; do
      pool="$pool
        server $h;"
    done
    echo "[lb] pool :$POOL_PORT round-robin over:$VPN_HOSTS"
    ;;
  *:*)
    pool="
        server $LB_MODE;"
    echo "[lb] pool :$POOL_PORT pinned to $LB_MODE"
    ;;
  *)
    pool="
        server vpn-$LB_MODE:8888;"
    echo "[lb] pool :$POOL_PORT pinned to region $LB_MODE"
    ;;
esac

# one listener per region: the exact address of that exit
region_listeners=""
for pair in $REGION_ENDPOINTS; do
  port="${pair%%=*}"
  target="${pair#*=}"
  region_listeners="$region_listeners

    server {
        listen ${port};
        proxy_pass ${target};
        proxy_connect_timeout 10s;
        proxy_timeout 600s;
    }"
  echo "[lb] region :$port -> $target"
done

# NOTE: 'zone' matters. Without shared memory the round-robin counter is per
# worker and short-lived connections all land on the first server.
cat > /etc/nginx/nginx.conf <<EOF
worker_processes auto;
error_log /dev/stderr warn;
pid /var/run/nginx.pid;

events { worker_connections 2048; }

stream {
    access_log off;

    upstream vpns {
        zone vpns 64k;${pool}
    }

    server {
        listen ${POOL_PORT};
        proxy_pass vpns;
        proxy_connect_timeout 10s;
        proxy_timeout 600s;
    }
${region_listeners}
}
EOF

exec nginx -g 'daemon off;'
