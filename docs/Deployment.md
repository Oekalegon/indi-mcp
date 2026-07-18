# Deployment

The INDI MCP server supports two ways of running, matching its two use cases:

* **`stdio` transport** — for local testing, e.g. running the server directly from a
  client on your development machine (Mac). The client owns the process's
  stdin/stdout, so this only works for a single client at a time and isn't suited to
  running as a background service.
* **`streamable-http` transport** — for production use on the INDI Device (the
  Raspberry Pi wired to the astrophotography gear). The server runs as a long-lived
  systemd service and is reachable over the network by any MCP client on the LAN.

`sse` is also available as a transport (inherited from the MCP SDK) but isn't the
recommended choice for either use case above.

## Running the server manually

```bash
# Local testing (stdio, default)
uv run indi-mcp

# Network-reachable, as used by the systemd service
uv run indi-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

`--host`/`--port` only apply to `sse`/`streamable-http`; they're ignored for `stdio`.

## Installing as a systemd service on the Raspberry Pi

These steps assume the Pi already has the `indiserver` binary and its drivers
installed (see the [Design Document](Design.md)), and that `uv` is available.

1. Create a dedicated system user and install directory:

   ```bash
   sudo useradd --system --home-dir /opt/indi-mcp --shell /usr/sbin/nologin indi-mcp
   sudo mkdir -p /opt/indi-mcp
   sudo chown indi-mcp:indi-mcp /opt/indi-mcp
   ```

   The service needs access to the serial/USB devices the INDI drivers talk to, so
   add the user to the group that owns them (`dialout` on Raspberry Pi OS):

   ```bash
   sudo usermod -aG dialout indi-mcp
   ```

2. Deploy the code and install dependencies into a venv under `/opt/indi-mcp`:

   ```bash
   sudo -u indi-mcp git clone <repo-url> /opt/indi-mcp/src
   cd /opt/indi-mcp/src
   sudo -u indi-mcp uv sync --no-dev
   sudo ln -s /opt/indi-mcp/src/.venv /opt/indi-mcp/.venv
   ```

   Adjust the paths above (and `WorkingDirectory`/`ExecStart` in the unit file, see
   below) if you prefer a different install layout, e.g. `pip install indi-mcp` into
   `/opt/indi-mcp/.venv` directly.

   Note that the code checkout (`/opt/indi-mcp/src`) is separate from the service's
   `WorkingDirectory` (`/opt/indi-mcp`). This matters for the built-in scripts shipped
   in the repo under `scripts/`: `script_store` looks for them at `./scripts` by
   default, which resolves against `WorkingDirectory`, not the checkout — so the
   shipped unit file sets `INDI_MCP_SCRIPTS_DIR` explicitly to point at
   `/opt/indi-mcp/src/scripts`. If you adjust the checkout path, update that
   environment variable to match. `rigs/`/`observatories/` don't need this, since
   those are user-authored config expected to live under `/opt/indi-mcp` itself, not
   shipped in the repo.

3. Install and enable the unit file:

   ```bash
   sudo cp deploy/indi-mcp.service /etc/systemd/system/indi-mcp.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now indi-mcp
   ```

4. Check it's up:

   ```bash
   sudo systemctl status indi-mcp
   journalctl -u indi-mcp -f
   ```

The service restarts automatically on failure and starts at boot
(`WantedBy=multi-user.target`).

### Hardening notes

The shipped unit file sets `ProtectSystem=strict` with `ReadWritePaths=/opt/indi-mcp`,
so the service can only write inside its own install directory. If frame storage or
other server state ends up living elsewhere, add that path to `ReadWritePaths` (or
relax `ProtectSystem`) — otherwise writes there will fail with a permission error.

### Updating

```bash
cd /opt/indi-mcp/src
sudo -u indi-mcp git pull
sudo -u indi-mcp uv sync --no-dev
sudo systemctl restart indi-mcp
```
