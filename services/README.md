# Local paper terminal services

This machine uses runit. `cointrade-scanner` and `cointrade-terminal` are installed
in `/etc/runit/sv` and enabled in `/etc/runit/runsvdir/default`. Runit starts them
after boot and restarts an exited process. They run as `jeremy`, listen only on
loopback, and use the existing SQLite database and saved subscription login.
The service environment excludes API keys; Goldsky uses its existing key file.

```sh
sudo sv status cointrade-scanner cointrade-terminal
sudo sv down cointrade-scanner cointrade-terminal
sudo sv up cointrade-scanner cointrade-terminal
```

Logs append to `data/gui.log` and `data/terminal.log`. The launcher scripts hold
exclusive locks to prevent duplicate workers. Supervision does not bypass data
freshness, wallet qualification, or trading rules. Interrupted Astra reviews
remain failed for inspection and are never automatically submitted again.

After editing a `run` file, install it with mode 0755 into the corresponding
`/etc/runit/sv/<service>/run` and restart that service when no Astra review is
in progress. An HTTP response confirms server availability; scanner timestamps
and the indexed head must also advance to confirm live data.
