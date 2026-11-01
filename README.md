# check-apcups

Read-only Nagios monitoring for an APC UPS exposed through **apcupsd NIS** using `apcaccess`. It reports UPS health and optional, independently polled host/service notification gates. The plugin never initiates UPS shutdown or changes UPS settings.

**Requirements:** Python 3.10+, an installed `apcaccess` executable (typically provided by apcupsd), Nagios Core or compatible monitoring, and network access to the configured NIS endpoint. No Python packages are required at runtime.

## Usage

```sh
python3 check_apcups.py --mode status --endpoint 192.0.2.10:3551 --state /var/lib/nagios/ups/state.json
python3 check_apcups.py --mode host-gate --state /var/lib/nagios/ups/state.json
python3 check_apcups.py --mode service-gate --state /var/lib/nagios/ups/state.json
```

`--mode` and `--state` are required; `status` also requires `--endpoint`. The default `--ttl` is 900 seconds and `--grace` is 300 seconds. Both are configurable for the **status** poll (`--ttl 900 --grace 300`). Gate checks read these settings from the shared state file. Each UPS requires its own state path and should be monitored using a single status writer.

## Example Nagios commands

```text
define command {
    command_name    check_apcups_status
    command_line    /usr/bin/python3 /usr/local/libexec/check_apcups.py --mode status --endpoint 192.0.2.10:3551 --state /var/lib/nagios/ups/state.json
}
define command {
    command_name    check_apcups_host_gate
    command_line    /usr/bin/python3 /usr/local/libexec/check_apcups.py --mode host-gate --state /var/lib/nagios/ups/state.json
}
define command {
    command_name    check_apcups_service_gate
    command_line    /usr/bin/python3 /usr/local/libexec/check_apcups.py --mode service-gate --state /var/lib/nagios/ups/state.json
}
```

Run the status poll at a suitable interval (for example, one minute) and configure the two gate helpers separately. Their checks **only read the cache**, avoiding duplicate UPS network requests. For Nagios notification dependencies, configure the synthetic gate host to suppress dependent hosts when DOWN (`notification_failure_criteria d`) and the gate service to suppress dependent services when CRITICAL (`notification_failure_criteria c`). Set execution failure criteria to `n` to continue checks; disable notifications *from* the synthetic helpers. Review and test the dependency graph for your own hosts and services before enabling suppression.

The status mode reports ONLINE or ONBATT, relevant battery status flags, charge, runtime, load, voltage, and Nagios performance data. ONBATT returns CRITICAL. A fresh confirmed outage activates both gates; after confirmed mains restoration, the configured grace period temporarily keeps suppression active. Missing, stale, unreadable or invalid state releases host suppression (host UP / exit 0); service gate reports UNKNOWN / exit 3. The independent status check reports telemetry problems.

## Statefile permissions and deployment

The monitoring user must own a private state directory with permissions **0700**. The plugin writes the JSON state atomically with permissions **0600**; readers should run under the same UID. Create the parent directory beforehand, for example:

```sh
sudo install -d -o nagios -g nagios -m 0700 /var/lib/nagios/ups
```

Only the direct parent directory is created by the plugin, not an arbitrary tree. Use a dedicated path per UPS. Avoid writable shared directories and do not run the plugin as root. A monitoring service account is recommended. Missing or invalid state fails open by design: notification suppression is lifted rather than hiding unrelated incidents.

**Security:** apcupsd NIS on TCP/3551 is normally unencrypted and unauthenticated. Restrict access at the firewall and use only trusted management networks or a secure tunnel. Do not expose NIS to the internet. The plugin invokes `apcaccess status` without a shell and with a 10-second subprocess timeout.

## Tests and license

```sh
python3 -m unittest discover -s tests -v
```

GitHub Actions runs tests on Python 3.10–3.13, Ruff correctness checks, Bandit (medium/high), and a tracked-file secrets scan. Licensed under **GPL-3.0-or-later**; see `LICENSE`.
