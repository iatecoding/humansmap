#!/usr/bin/env bash
#
# Lock the origin so that ports 80/443 only accept *inbound* connections from
# Cloudflare's published IP ranges.
#
# Why this exists
# ---------------
# When a site is proxied through Cloudflare, the origin server usually still
# listens on its public IP. Anyone who learns that IP can bypass Cloudflare
# entirely (WAF, bot management, caching, DDoS protection) by connecting
# straight to the origin. This script closes that hole.
#
# Why not just `ufw`?
# -------------------
# Docker publishes container ports by writing its own iptables rules. Those
# rules are evaluated before ufw's, so a plain `ufw deny 80` does NOT filter a
# Docker-published port. The correct place to filter Docker-published ports is
# the `DOCKER-USER` chain, which is what this script uses.
#
# Why match on the ingress interface?
# -----------------------------------
# `DOCKER-USER` sits in `FORWARD`, which also sees *outbound* container traffic.
# Matching only on `-i <public-iface>` keeps the allow/deny to traffic arriving
# from the internet, so containers can still make outbound HTTPS calls (e.g.
# the backend calling Wikidata / Wikipedia / SEC).
#
# How it runs without root
# ------------------------
# The host's iptables lives in the host network namespace, which needs
# privileges the `deploy` account does not have. If the account is in the
# `docker` group it can run a short-lived privileged helper container that
# enters the host namespaces (via nsenter) and applies the rules there. Set
# RUN_AS_ROOT=1 to call iptables directly if you do have root.
#
# Idempotent: it flushes and rebuilds its own DOCKER-USER rules each run.
# Safe: it self-rolls-back (flushes DOCKER-USER) if the site stops responding.
#
# Usage:
#   scripts/lock_origin_to_cloudflare.sh
#
# Environment overrides:
#   SITE_URL        public URL used for the post-apply health check
#                   (default https://humansmap.com/)
#   IFACE           public ingress interface (default: the default-route iface)
#   HELPER_IMAGE    helper container image (default alpine:3.19)
#   RUN_AS_ROOT=1   run iptables directly instead of via a helper container
#
# Suggested schedule (see docs/DEPLOYMENT.md): @reboot and weekly, because
# iptables state does not survive a reboot and Cloudflare's ranges change.

set -euo pipefail

# cron runs with a minimal PATH; make sure docker/ip/curl resolve.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

SITE_URL="${SITE_URL:-https://humansmap.com/}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.19}"
CF4_URL="${CF4_URL:-https://www.cloudflare.com/ips-v4}"
CF6_URL="${CF6_URL:-https://www.cloudflare.com/ips-v6}"
RUN_AS_ROOT="${RUN_AS_ROOT:-0}"
IFACE="${IFACE:-$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')}"
IFACE="${IFACE:-eth0}"
export IFACE

CIDR_RE='^[0-9a-fA-F:.]+/[0-9]{1,3}$'

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> fetching Cloudflare ranges"
curl -fsS "$CF4_URL" -o "$tmp/v4"
curl -fsS "$CF6_URL" -o "$tmp/v6"
grep -Ev '^\s*(#|$)' "$tmp/v4" | grep -Eq "$CIDR_RE" || { echo "ERROR: no valid IPv4 ranges" >&2; exit 1; }
grep -Ev '^\s*(#|$)' "$tmp/v6" | grep -Eq "$CIDR_RE" || { echo "ERROR: no valid IPv6 ranges" >&2; exit 1; }
echo "    $(grep -c . "$tmp/v4") IPv4 + $(grep -c . "$tmp/v6") IPv6 ranges; ingress iface: $IFACE"

# Rule builders. Injected into the privileged helper (or eval'd as root).
# Assumes an `ipt` function is already defined that runs a command in the host
# network namespace.
RULES='
apply_family() {
  cmd="$1"; ranges="$2"
  if ipt "$cmd" -nL DOCKER-USER >/dev/null 2>&1; then :; else
    echo "    ($cmd: no DOCKER-USER chain, skipping)"; return 0
  fi
  ipt "$cmd" -F DOCKER-USER
  ipt "$cmd" -A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  while read -r cidr; do
    [ -n "$cidr" ] && ipt "$cmd" -A DOCKER-USER -i "$IFACE" -s "$cidr" -p tcp -m multiport --dports 80,443 -j RETURN
  done < "$ranges"
  ipt "$cmd" -A DOCKER-USER -i "$IFACE" -p tcp -m multiport --dports 80,443 -j DROP
  ipt "$cmd" -A DOCKER-USER -j RETURN
  echo "    $cmd: $(ipt "$cmd" -S DOCKER-USER | grep -c multiport || true) multiport rules"
}
flush_family() {
  ipt "$1" -nL DOCKER-USER >/dev/null 2>&1 && ipt "$1" -F DOCKER-USER || true
}
'

flush() {
  if [ "$RUN_AS_ROOT" = "1" ]; then
    ipt() { "$@"; }; flush_family iptables; flush_family ip6tables
  else
    docker run --rm --privileged --pid=host --net=host "$HELPER_IMAGE" sh -c '
      apk add --no-cache -q util-linux >/dev/null 2>&1 || true
      ipt() { nsenter -t 1 -m -n -- "$@"; }
      ipt iptables -F DOCKER-USER 2>/dev/null || true
      ipt ip6tables -F DOCKER-USER 2>/dev/null || true
    '
  fi
}

if [ "$RUN_AS_ROOT" = "1" ]; then
  echo "==> applying rules directly (root)"
  ipt() { "$@"; }
  eval "$RULES"
  apply_family iptables "$tmp/v4"
  apply_family ip6tables "$tmp/v6"
else
  command -v docker >/dev/null || { echo "ERROR: docker not found and RUN_AS_ROOT != 1" >&2; exit 1; }
  echo "==> applying rules via privileged helper container"
  docker run --rm --privileged --pid=host --net=host \
    -e IFACE="$IFACE" -v "$tmp":/cf:ro "$HELPER_IMAGE" sh -c '
      apk add --no-cache -q util-linux >/dev/null 2>&1 || apk add --no-cache util-linux >/dev/null 2>&1 || true
      command -v nsenter >/dev/null || { echo "ERROR: nsenter unavailable in helper" >&2; exit 1; }
      ipt() { nsenter -t 1 -m -n -- "$@"; }
      '"$RULES"'
      apply_family iptables /cf/v4
      apply_family ip6tables /cf/v6
    '
fi

echo "==> health check: $SITE_URL (through Cloudflare)"
ok=0
for _ in 1 2 3 4 5; do
  if curl -fsS --max-time 10 -H 'Cache-Control: no-cache' -o /dev/null "$SITE_URL?lock_origin_cb=$(date +%s)"; then ok=1; break; fi
  sleep 3
done

if [ "$ok" != "1" ]; then
  echo "ERROR: $SITE_URL did not respond after applying rules — rolling back" >&2
  flush
  exit 1
fi

echo "==> origin locked to Cloudflare inbound (ports 80/443 on $IFACE). Other traffic untouched."
