#!/usr/bin/env python3
# Monitor apcupsd UPS health and cached power notification gates.
# Author: Stijn Jonker (sjcjonker) | https://github.com/sjcjonker
# Usage: python3 check_apcups.py --mode status --endpoint 192.0.2.10:3551 --state /var/lib/nagios/ups/state.json
# Requires apcaccess; status TTL: 900s, recovery grace: 300s. Help: --help.
# Nagios exit codes: 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN.
"""Monitor apcupsd and supply fail-open Nagios power notification gates."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

DEFAULT_TTL = 900
DEFAULT_GRACE = 300


class UPSFailure(Exception):
    pass


def read_state(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o077):
                return None
            text = source.read(8193)
        if len(text) > 8192:
            return None
        value = json.loads(text)
        if not isinstance(value, dict) or value.get('version') != 1:
            return None
        return value
    except (OSError, ValueError, UnicodeError):
        return None


def save_state(path, value):
    path.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = path.parent.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077):
        raise UPSFailure('UPS state directory must be owned by nagios and mode 0700')
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def number(fields, name, lower, upper):
    try:
        value = float(fields[name].split()[0])
    except (KeyError, IndexError, ValueError):
        raise UPSFailure('missing or invalid UPS measurement: ' + name)
    if not math.isfinite(value) or not lower <= value <= upper:
        raise UPSFailure('out-of-range UPS measurement: ' + name)
    return value


def parse_status(text, now):
    if len(text) > 65536:
        raise UPSFailure('apcupsd response exceeds size limit')
    fields = {}
    for line in text.splitlines():
        match = re.match(r'^([A-Z][A-Z0-9 ]*?)\s*:\s*(.*)$', line)
        if match:
            key, value = match.groups()
            if key in fields:
                raise UPSFailure('duplicate apcupsd status field')
            fields[key] = value.strip()
    flags = set(fields.get('STATUS', '').split())
    if not flags or 'COMMLOST' in flags:
        raise UPSFailure('UPS communication lost or status missing')
    if ('ONLINE' in flags) == ('ONBATT' in flags):
        raise UPSFailure('UPS mains state is ambiguous')
    try:
        stamp = dt.datetime.strptime(fields['DATE'], '%Y-%m-%d %H:%M:%S %z').timestamp()
    except (KeyError, ValueError):
        raise UPSFailure('UPS sample timestamp missing or invalid')
    if not -60 <= now - stamp <= 120:
        raise UPSFailure('UPS sample timestamp is stale or in the future')
    values = dict(charge=number(fields, 'BCHARGE', 0, 100),
                  runtime=number(fields, 'TIMELEFT', 0, 100000),
                  load=number(fields, 'LOADPCT', 0, 1000),
                  voltage=number(fields, 'LINEV', 0, 1000))
    return flags, values


def fresh(state, now):
    if not state or state.get('known') is not True:
        return False
    stamp = state.get('sampled_at')
    ttl = state.get('ttl', DEFAULT_TTL)
    grace = state.get('grace', DEFAULT_GRACE)
    if (type(ttl) is not int or not 1 <= ttl <= 86400
            or type(grace) is not int or not 0 <= grace <= 86400):
        return False
    return (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
            and math.isfinite(stamp) and 0 <= now - stamp <= ttl
            and isinstance(state.get('onbattery'), bool))


def advance(previous, onbattery, now,
            ttl=DEFAULT_TTL, grace=DEFAULT_GRACE):
    hold = 0
    if not onbattery and fresh(previous, now):
        if previous['onbattery']:
            hold = now + grace
        else:
            candidate = previous.get('recover_until', 0)
            started = previous.get('recovery_started', 0)
            duration = previous.get('recovery_grace', 0)
            if (type(candidate) in (int, float) and not isinstance(candidate, bool)
                    and math.isfinite(candidate)
                    and type(started) in (int, float) and not isinstance(started, bool)
                    and math.isfinite(started)
                    and type(duration) is int and 0 < duration <= 86400
                    and candidate == started + duration and now < candidate):
                hold = candidate
    recovery_started = 0
    recovery_grace = 0
    if hold:
        if previous and previous.get('onbattery') is False:
            recovery_started = previous.get('recovery_started', 0)
            recovery_grace = previous.get('recovery_grace', 0)
        else:
            recovery_started = now
            recovery_grace = grace

    return dict(version=1, sampled_at=now, known=True,
                onbattery=onbattery, recover_until=hold,
                recovery_started=recovery_started,
                recovery_grace=recovery_grace,
                ttl=ttl, grace=grace)


def gate(state, now, host=False):
    if not fresh(state, now):
        # Native hostdependencies have no UNKNOWN state. Return UP to release
        # suppression, while the independent UPS Status service reports failure.
        return (0 if host else 3), 'UPS state unknown/stale; notification suppression OFF'
    if state['onbattery']:
        return 2, 'Confirmed mains failure; notification suppression ON'
    hold = state.get('recover_until', 0)
    started = state.get('recovery_started', 0)
    duration = state.get('recovery_grace', 0)
    if (type(hold) in (int, float) and not isinstance(hold, bool)
            and math.isfinite(hold)
            and type(started) in (int, float) and not isinstance(started, bool)
            and math.isfinite(started)
            and type(duration) is int and 0 < duration <= 86400
            and now < hold == started + duration):
        return 2, 'Mains restored; startup grace active for %d seconds' % math.ceil(hold - now)
    return 0, 'Mains present; notification suppression OFF'


def health(flags, values):
    problems = []
    code = 0
    if 'ONBATT' in flags:
        code = 2
        problems.append('Mains failure: UPS ONBATT')
    for flag in ('LOWBATT', 'NOBATT', 'OVERLOAD', 'SHUTTINGDOWN'):
        if flag in flags:
            code = 2
            problems.append(flag)
    if 'REPLACEBATT' in flags:
        code = max(code, 1)
        problems.append('REPLACEBATT')
    if values['charge'] <= 10 or values['runtime'] <= 3:
        code = 2
        problems.append('battery charge/runtime critically low')
    elif values['charge'] <= 25 or values['runtime'] <= 5:
        code = max(code, 1)
        problems.append('battery charge/runtime low')
    summary = '; '.join(problems) or 'UPS ONLINE'
    summary += '; battery %.1f%%, runtime %.1f min, load %.1f%%, input %.1f V' % (
        values['charge'], values['runtime'], values['load'], values['voltage'])
    summary += ' | battery=%.1f%%;25:;10:;0;100 runtime=%.1fmin;5:;3:;0 load=%.1f%% input_voltage=%.1fV' % (
        values['charge'], values['runtime'], values['load'], values['voltage'])
    return code, summary


def collect(path, now, endpoint, ttl=DEFAULT_TTL,
            grace=DEFAULT_GRACE):
    previous = read_state(path)
    try:
        command = next((str(p) for p in (Path('/sbin/apcaccess'), Path('/usr/sbin/apcaccess'),
                                         Path('/usr/bin/apcaccess')) if p.is_file()), None)
        if not command:
            raise UPSFailure('apcaccess missing; install apcupsd with its daemon masked')
        result = subprocess.run([command, 'status', endpoint], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode:
            raise UPSFailure('apcaccess failed; verify Nagios TCP 3551 access to the apcupsd endpoint')
        flags, values = parse_status(result.stdout, now)
        save_state(path, advance(previous, 'ONBATT' in flags, now,
                                 ttl=ttl, grace=grace))
        return health(flags, values)
    except (UPSFailure, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        # Invalidate an earlier ONBATT sample immediately, not just after TTL.
        try:
            save_state(path, dict(version=1, sampled_at=now, known=False))
        except (OSError, UPSFailure):
            pass  # Unwritable old state expires at TTL regardless.
        reason = str(exc) if isinstance(exc, UPSFailure) else type(exc).__name__
        return 3, 'UPS telemetry unavailable: ' + reason


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('status', 'host-gate', 'service-gate'), required=True)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--endpoint', help='apcupsd NIS host:port')
    parser.add_argument('--ttl', type=int, default=DEFAULT_TTL)
    parser.add_argument('--grace', type=int, default=DEFAULT_GRACE)
    args = parser.parse_args(argv)
    if args.mode == 'status' and not args.endpoint:
        parser.error('--endpoint is required in status mode')
    if not 1 <= args.ttl <= 86400:
        parser.error('--ttl must be between 1 and 86400 seconds')
    if not 0 <= args.grace <= 86400:
        parser.error('--grace must be between 0 and 86400 seconds')
    now = time.time()
    if args.mode == 'status':
        code, output = collect(args.state, now, args.endpoint,
                               ttl=args.ttl, grace=args.grace)
    else:
        code, output = gate(read_state(args.state), now, host=args.mode == 'host-gate')
    print(('OK', 'WARNING', 'CRITICAL', 'UNKNOWN')[code] + ' - ' + output)
    return code


if __name__ == '__main__':
    sys.exit(main())
