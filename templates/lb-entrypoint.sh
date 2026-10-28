#!/bin/sh
set -eu

# Runs at container init. Decides the LB's behaviour from LB_MODE:
#   rr | round-robin  -> balance across every host in VPN_HOSTS
#   <region>          -> pin to vpn-<region>:8888
#   <host:port>       -> pin to that literal upstream
: "${LB_MODE:=rr}"
: "${VPN_HOSTS:=}"
: "${LISTEN_PORT:=8888}"

case "$LB_MODE" in
  rr|round-robin)
    if [ -z "$VPN_HOSTS" ]; then
      echo "LB_MODE=$LB_MODE needs VPN_HOSTS (space separated host:port list)" >&2
      exit 1
    fi
    servers=""
    for h in $VPN_HOSTS; do
      servers="$servers
        server $h;"
    done
    echo "[lb] mode=round-robin over:$VPN_HOSTS"
    ;;
  *:*)
    servers="
        server $LB_MODE;"
    echo "[lb] mode=pinned to $LB_MODE"
    ;;
  *)
    servers="
        server vpn-$LB_MODE:8888;"
    echo "[lb] mode=pinned to region $LB_MODE"
    ;;
esac

# NOTE: 'zone' matters. Without shared memory the round-robin counter is
# per worker and short-lived connections all land on the first server.
cat > /etc/nginx/nginx.conf <<EOF
worker_processes auto;
error_log /dev/stderr warn;
pid /var/run/nginx.pid;

events { worker_connections 2048; }

stream {
    access_log off;

    upstream vpns {
        zone vpns 64k;${servers}
    }

    server {
        listen ${LISTEN_PORT};
        proxy_pass vpns;
        proxy_connect_timeout 10s;
        proxy_timeout 600s;
    }
}
EOF

exec nginx -g 'daemon off;'
