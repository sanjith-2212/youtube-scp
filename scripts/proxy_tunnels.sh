#!/usr/bin/env bash
# SOCKS5 tunnels over SSH, one local port per egress route.
#
# The pipeline hands these to yt-dlp as --proxy socks5h://127.0.0.1:<port>.
# They do not survive a reboot (or an ssh drop past the keepalive), so run this
# before a scrape and re-run it to repair: it is idempotent, replacing only the
# tunnels that are actually down.
#
# Measured 2026-11-02: YouTube shapes each connection to ~0.35-0.45 MB/s no
# matter which route it leaves by, so these do not speed up any single
# download. They spread the request count across IPs, which is what the bot
# check actually counts.
#
#   ./scripts/proxy_tunnels.sh          # bring up / repair
#   ./scripts/proxy_tunnels.sh status   # show state and exit IPs
#   ./scripts/proxy_tunnels.sh stop     # tear down

set -uo pipefail

# port:ssh-host
ROUTES=(
  "1081:fake-datagen-server"
  "1082:jarvis-server-2"
)

SSH_OPTS=(
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o ExitOnForwardFailure=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=15
)

port_live() {  # a listening local port is necessary but not sufficient
  timeout 5 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null
}

exit_ip() {
  curl -s --max-time 10 --proxy "socks5h://127.0.0.1:$1" https://api.ipify.org 2>/dev/null
}

case "${1:-up}" in
  stop)
    for route in "${ROUTES[@]}"; do
      port="${route%%:*}"
      pkill -f "ssh .*-D ${port} " && echo "stopped :${port}"
    done
    ;;

  status)
    echo "  direct -> $(curl -s --max-time 10 https://api.ipify.org)"
    for route in "${ROUTES[@]}"; do
      port="${route%%:*}"; host="${route##*:}"
      if port_live "$port"; then
        echo "  :${port} (${host}) -> $(exit_ip "$port")"
      else
        echo "  :${port} (${host}) -> DOWN"
      fi
    done
    ;;

  up|*)
    for route in "${ROUTES[@]}"; do
      port="${route%%:*}"; host="${route##*:}"
      if port_live "$port" && [ -n "$(exit_ip "$port")" ]; then
        echo "  :${port} (${host}) already up"
        continue
      fi
      # Half-open tunnel: listener present but no traffic passing. Clear it,
      # otherwise the new ssh fails to bind and the route stays dead.
      pkill -f "ssh .*-D ${port} " 2>/dev/null
      sleep 1
      if ssh "${SSH_OPTS[@]}" -N -f -D "$port" "$host"; then
        sleep 2
        ip="$(exit_ip "$port")"
        if [ -n "$ip" ]; then
          echo "  :${port} (${host}) -> ${ip}"
        else
          echo "  :${port} (${host}) -> UP but no egress; check the host" >&2
        fi
      else
        echo "  :${port} (${host}) -> FAILED to open" >&2
      fi
    done
    ;;
esac
