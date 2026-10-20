# Cross-host SSH pivoting

Resolve `$GW_HOST`, `$SSH_USER`, `$SSH_KEY` from `lab.env`. **Prefer key auth.** If only a password
works, have the user run the interactive command themselves rather than Claude embedding the secret.

## Key auth (preferred)

```bash
# Generate a lab key once (user runs this)
ssh-keygen -t ed25519 -f ~/.ssh/lab_ed25519 -C lab
# Install it on the gateway (user runs this, enters password once)
ssh-copy-id -i ~/.ssh/lab_ed25519.pub "$SSH_USER@$GW_HOST"
```

Then every later call is passwordless:

```bash
ssh -i "$SSH_KEY" "$SSH_USER@$GW_HOST" "uptime"
```

## Jump host (`-J`) — one hop to an internal node

```bash
ssh -i "$SSH_KEY" -J "$SSH_USER@$GW_HOST" "$SSH_USER@172.16.1.51" "docker ps"
```

Chain multiple hops by comma-separating jump hosts:

```bash
ssh -i "$SSH_KEY" -J "$SSH_USER@$GW_HOST,$SSH_USER@172.16.1.51" "$SSH_USER@10.0.0.9" "hostname"
```

## SOCKS proxy — browse a whole isolated segment

```bash
ssh -i "$SSH_KEY" -D 1080 -N "$SSH_USER@$GW_HOST"      # leave running; proxy on localhost:1080
# then: curl --socks5-hostname localhost:1080 http://172.16.1.51/
```

## Copy a file across a hop

```bash
scp -i "$SSH_KEY" -o ProxyJump="$SSH_USER@$GW_HOST" ./rules.sh "$SSH_USER@172.16.1.51:/tmp/"
```

## Host-key checking

`-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` skips host-key verification. Acceptable
only on a throwaway, isolated lab where the hosts are rebuilt often — it defeats MITM protection, so
never use it for anything reachable from an untrusted network.

## Running these from Cowork/cloud

SSH to a lab IP only works from a shell that can actually reach the lab — i.e. the device/remote
shell on the user's machine, not Claude's cloud sandbox (SKILL.md §0). If an SSH call hangs or is
refused, check the execution path before blaming the host.
