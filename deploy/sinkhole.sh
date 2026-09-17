#!/usr/bin/env bash
# Sinkhole default route and no upstream DNS (README section 6.1).
# Any non-LAN packet is routed to a dummy interface, so it passes the nftables output hook and is
# counted and dropped. Artifact only: run it by hand on the target server after review.
set -euo pipefail

ip link add egress0 type dummy 2>/dev/null || true
ip link set egress0 up
ip route replace default dev egress0
ip -6 route replace default dev egress0

systemctl disable --now systemd-resolved || true
rm -f /etc/resolv.conf
touch /etc/resolv.conf

# Internal names (directory server, log host) belong in /etc/hosts.
# Verify: `ip route` shows only the LAN subnet and `default dev egress0`;
#         `nft list ruleset` shows the sovereign table with output policy drop.

# Second, independent counter: record every connect() for egressd's auditlog collector.
if command -v auditctl >/dev/null; then
  auditctl -a always,exit -F arch=b64 -S connect -k wb_connect
fi
