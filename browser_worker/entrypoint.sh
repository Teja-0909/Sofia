#!/bin/sh
# Runs briefly as root to lock this container's network namespace, then drops
# ALL capabilities before starting Python/Chromium as uid 10001. Never use host
# network mode: these rules must affect only this dedicated container namespace.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Network bootstrap requires container root' >&2; exit 1; }
[ -n "${BROWSER_WORKER_TOKEN:-}" ] || { echo 'Worker token is required' >&2; exit 1; }
# Fixed proxy destination matches policy.py and compose; no user-supplied shell IP.
iptables -w -P OUTPUT DROP
iptables -w -P INPUT DROP
iptables -w -P FORWARD DROP
iptables -w -F OUTPUT
iptables -w -F INPUT
iptables -w -F FORWARD
iptables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -w -A OUTPUT -p tcp -d 172.30.91.2 --dport 3128 -m conntrack --ctstate NEW -j ACCEPT
iptables -w -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -w -A INPUT -p tcp --dport 8080 -m conntrack --ctstate NEW -j ACCEPT
# IPv6 is disabled in compose; explicit deny as a second boundary if it exists.
ip6tables -w -P OUTPUT DROP
ip6tables -w -P INPUT DROP
ip6tables -w -P FORWARD DROP
ip6tables -w -F OUTPUT
ip6tables -w -F INPUT
ip6tables -w -F FORWARD
# /run is an empty root-owned tmpfs. Marker is not accepted from an env switch.
umask 022
printf 'sofia-egress-v1 172.30.91.2:3128\n' > /run/sofia-network.lock
chmod 0444 /run/sofia-network.lock
exec setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs \
    --inh-caps=-all --ambient-caps=-all --bounding-set=-all \
    python -m browser_worker.server
