# Stateless iptables firewalls

A "stateless" lab firewall bans connection-tracking matches in the `filter` table: no `-m conntrack`,
no `-m state`, no `--state ESTABLISHED,RELATED`. Return traffic is permitted by explicit reverse
rules instead. NAT still uses conntrack, but only inside the `nat` table, which is fine.

## Core principles

1. **Default DROP** on all three chains.
2. **Symmetrical pairs**: one forward rule + one reverse rule per allowed flow.
3. **Reverse rules match return TCP without state** using `! --syn` (packets that aren't a new
   connection's SYN) or explicit `--tcp-flags`.
4. **NAT lives in the `nat` table**, independent of `filter`.

## Skeleton

```bash
#!/bin/bash
set -e
WAN_IF="ens33"          # from lab.env
# wipe
iptables -F; iptables -X
iptables -t nat -F; iptables -t nat -X
# default DROP
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP
# loopback
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# --- management SSH to the firewall itself (so you don't lock out) ---
iptables -A INPUT  -i ens160.15 -p tcp --dport 22 -j ACCEPT
iptables -A OUTPUT -o ens160.15 -p tcp --sport 22 ! --syn -j ACCEPT

# --- example: Admin (172.16.1.18) -> DMZ (192.168.1.0/26) SSH ---
iptables -A FORWARD -s 172.16.1.18     -d 192.168.1.0/26 -p tcp --dport 22 -j ACCEPT
iptables -A FORWARD -s 192.168.1.0/26  -d 172.16.1.18     -p tcp --sport 22 ! --syn -j ACCEPT

# --- example: Users -> Internet (DNS + HTTP/S), stateless ---
for P in 53 80 443; do
  iptables -A FORWARD -i ens160.10 -o "$WAN_IF" -p tcp --dport $P -j ACCEPT
  iptables -A FORWARD -i "$WAN_IF" -o ens160.10 -p tcp --sport $P ! --syn -j ACCEPT
done
# UDP DNS (no SYN concept; pair by port only)
iptables -A FORWARD -i ens160.10 -o "$WAN_IF" -p udp --dport 53 -j ACCEPT
iptables -A FORWARD -i "$WAN_IF" -o ens160.10 -p udp --sport 53 -j ACCEPT

# --- NAT (conntrack confined to nat table — allowed) ---
iptables -t nat -A POSTROUTING -o "$WAN_IF" -j MASQUERADE
```

Save `$RULES_SCRIPT` somewhere the restore tool can find it (see SKILL.md §5) and
`iptables-save > /etc/iptables/rules.v4`.

## Applying rules remotely without locking yourself out

Losing SSH mid-edit is the #1 way to brick a remote gateway. Belt and braces:

1. **Snapshot the VM first** (`vmrun ... snapshot`), so a revert is always possible from the host.
2. **Arm a dead-man's switch** before applying:
   ```bash
   echo "/usr/local/bin/fw-killswitch" | at now + 3 minutes   # auto-opens in 3 min
   ```
3. Apply your ruleset.
4. **Prove you still have access** (open a *second* SSH session; run a command).
5. Cancel the timer: `atq` then `atrm <job-id>`.

If step 4 fails, the dead-man's switch opens the firewall for you; reconnect and fix the rules.

## Why `! --syn`

A new TCP connection opens with a SYN (no ACK). Every later packet of that connection has ACK set.
`! --syn` therefore matches "any TCP packet that isn't a brand-new connection attempt" — which is
exactly the return traffic you want to allow without tracking state. For the strict version:
`--tcp-flags SYN,ACK,FIN,RST ACK` (only the ACK bit set).
