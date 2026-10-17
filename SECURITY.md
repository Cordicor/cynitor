# Security

Cynitor sends commands to the nodes on a CAN bus: it can restart them, write
their registers and flash new firmware. A flaw in it can reach real hardware.

## Reporting a vulnerability

Please report it privately, not in a public issue. Use **Report a
vulnerability** on the repository's
[Security tab](https://github.com/OpenCyphal-Garage/cynitor/security). Say
which version you ran (`cynitor-server --version`), how it was started, and
what an attacker could do with the flaw.

You will get an answer within a week. A fix goes out in a release, whose notes
name the problem once users can update.

## Supported versions

Only the latest release gets security fixes.

## Running it safely

- Cynitor listens on `127.0.0.1` by default. To reach it from another machine,
  set `CYNITOR_AUTH_TOKEN` (it refuses to bind beyond loopback without one)
  and, beyond a trusted network, put a TLS reverse proxy in front: it serves
  plain HTTP.
- The packaged systemd service runs as an unprivileged user of its own.
- `--listen-only` watches a bus without sending anything on it.
