import contextlib
import io
import datetime as dt
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_apcups', ROOT / 'check_apcups.py')
ups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ups)
FIXED_NOW = 1792529000


def sample(status='ONLINE', charge=100, runtime=24.8, timestamp=FIXED_NOW):
    date = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S %z')
    return '\n'.join(('STATUS : ' + status, 'DATE : ' + date,
                      'BCHARGE : %s Percent' % charge,
                      'TIMELEFT : %s Minutes' % runtime,
                      'LOADPCT : 43.0 Percent', 'LINEV : 234.0 Volts'))


class UPSChecks(unittest.TestCase):
    def test_actual_field_shapes_and_online_health(self):
        flags, values = ups.parse_status(sample(), FIXED_NOW)
        self.assertEqual(values['runtime'], 24.8)
        self.assertEqual(ups.health(flags, values)[0], 0)
        self.assertEqual(ups.gate(ups.advance(None, False, FIXED_NOW), FIXED_NOW)[0], 0)

    def test_onbattery_is_critical_and_suppresses_both_notification_types(self):
        flags, values = ups.parse_status(sample('ONBATT'), FIXED_NOW)
        self.assertEqual(ups.health(flags, values)[0], 2)
        state = ups.advance(None, True, FIXED_NOW)
        self.assertEqual(ups.gate(state, FIXED_NOW, host=True)[0], 2)
        self.assertEqual(ups.gate(state, FIXED_NOW)[0], 2)

    def test_replace_battery_and_low_charge_online_do_not_suppress(self):
        for flags in ('ONLINE REPLACEBATT', 'ONLINE LOWBATT'):
            status, values = ups.parse_status(sample(flags, charge=5), FIXED_NOW)
            self.assertGreater(ups.health(status, values)[0], 0)
            self.assertEqual(ups.gate(ups.advance(None, False, FIXED_NOW), FIXED_NOW)[0], 0)

    def test_communication_loss_and_contradictory_status_are_not_outage(self):
        for flags in ('COMMLOST', 'ONLINE COMMLOST', '', 'ONLINE ONBATT', 'REPLACEBATT'):
            with self.assertRaises(ups.UPSFailure):
                ups.parse_status(sample(flags), FIXED_NOW)

    def test_sample_age_and_invalid_numbers_fail_unknown(self):
        for text in (sample(timestamp=FIXED_NOW-121), sample(timestamp=FIXED_NOW+61),
                     sample(charge='nan'), sample(runtime='inf'), sample(charge=101)):
            with self.assertRaises(ups.UPSFailure):
                ups.parse_status(text, FIXED_NOW)

    def test_recovery_grace_expires_without_more_ups_polls(self):
        outage = ups.advance(None, True, FIXED_NOW)
        recovered = ups.advance(outage, False, FIXED_NOW+10)
        self.assertEqual(ups.gate(recovered, FIXED_NOW+309)[0], 2)
        self.assertEqual(ups.gate(recovered, FIXED_NOW+310)[0], 0)

    def test_online_polls_do_not_extend_recovery_window(self):
        recovered = ups.advance(ups.advance(None, True, FIXED_NOW), False, FIXED_NOW+10)
        again = ups.advance(recovered, False, FIXED_NOW+20)
        self.assertEqual(again['recover_until'], FIXED_NOW+310)

    def test_stale_missing_unknown_or_future_cache_does_not_suppress(self):
        state = ups.advance(None, True, FIXED_NOW)
        for value, clock in ((None, FIXED_NOW), ({'known': False}, FIXED_NOW),
                             (state, FIXED_NOW+ups.DEFAULT_TTL+1), (state, FIXED_NOW-1)):
            self.assertEqual(ups.gate(value, clock, host=True)[0], 0)
            self.assertEqual(ups.gate(value, clock)[0], 3)

    def test_atomic_private_cache_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state' / 'state.json'
            state = ups.advance(None, True, FIXED_NOW)
            ups.save_state(path, state)
            self.assertEqual(ups.read_state(path), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text('invalid')
            self.assertIsNone(ups.read_state(path))

    def test_failed_collection_invalidates_previous_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state' / 'state.json'
            ups.save_state(path, ups.advance(None, True, FIXED_NOW-10))
            with patch.object(ups.Path, 'is_file', return_value=True), \
                    patch.object(ups.subprocess, 'run', side_effect=ups.subprocess.TimeoutExpired('apcaccess', 10)):
                code, text = ups.collect(path, FIXED_NOW, '192.0.2.10:3551')
            self.assertEqual(code, 3)
            self.assertEqual(ups.gate(ups.read_state(path), FIXED_NOW, host=True)[0], 0)

    def test_custom_endpoint_is_passed_to_apcaccess(self):
        with tempfile.TemporaryDirectory() as folder:
            result = MagicMock(returncode=0, stdout=sample())
            with patch.object(ups.Path, 'is_file', return_value=True), \
                    patch.object(ups.subprocess, 'run', return_value=result) as run:
                self.assertEqual(ups.collect(Path(folder) / 'state' / 'state.json', FIXED_NOW,
                                             '192.0.2.10:3551')[0], 0)
            self.assertEqual(run.call_args.args[0][-1], '192.0.2.10:3551')

    def test_collection_uses_readonly_apcaccess_fixed_endpoint_and_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state' / 'state.json'
            result = MagicMock(returncode=0, stdout=sample('ONBATT'))
            with patch.object(ups.Path, 'is_file', return_value=True), \
                    patch.object(ups.subprocess, 'run', return_value=result) as run:
                self.assertEqual(ups.collect(path, FIXED_NOW, '192.0.2.10:3551')[0], 2)
            self.assertEqual(run.call_args.args[0], ['/sbin/apcaccess', 'status', '192.0.2.10:3551'])
            self.assertEqual(run.call_args.kwargs['timeout'], 10)


    def test_configurable_ttl(self):
        state = ups.advance(None, True, FIXED_NOW, ttl=60)
        self.assertEqual(ups.gate(state, FIXED_NOW + 60)[0], 2)
        self.assertEqual(ups.gate(state, FIXED_NOW + 61)[0], 3)

    def test_configurable_grace(self):
        outage = ups.advance(None, True, FIXED_NOW, grace=120)
        recovered = ups.advance(
            outage, False, FIXED_NOW + 10, grace=120
        )
        self.assertEqual(ups.gate(recovered, FIXED_NOW + 129)[0], 2)
        self.assertEqual(ups.gate(recovered, FIXED_NOW + 130)[0], 0)

    def test_recovery_survives_multiple_polls_after_grace_change(self):
        outage = ups.advance(None, True, FIXED_NOW)
        recovered = ups.advance(outage, False, FIXED_NOW + 10)
        for seconds in (20, 30, 40):
            recovered = ups.advance(recovered, False, FIXED_NOW + seconds, grace=120)
            self.assertEqual(recovered['recover_until'], FIXED_NOW + 310)
            self.assertEqual(ups.gate(recovered, FIXED_NOW + seconds)[0], 2)
        self.assertEqual(ups.gate(recovered, FIXED_NOW + 310)[0], 0)

    def test_malformed_recovery_deadline_fails_open(self):
        state = ups.advance(ups.advance(None, True, FIXED_NOW), False, FIXED_NOW + 10)
        for invalid in ('bad', None, [], {}, True, float('nan')):
            with self.subTest(deadline=repr(invalid)):
                broken = dict(state, recover_until=invalid)
                self.assertEqual(ups.gate(broken, FIXED_NOW + 20, host=True)[0], 0)
                self.assertEqual(ups.gate(broken, FIXED_NOW + 20)[0], 0)
                self.assertEqual(ups.advance(broken, False, FIXED_NOW + 20)['recover_until'], 0)

    def test_wrong_owner_state_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text('{}')
            with patch.object(ups.os, 'geteuid', return_value=os.geteuid() + 1):
                self.assertIsNone(ups.read_state(path))

    def test_missing_required_cli_arguments(self):
        for args in (
            ['--mode', 'status', '--state', '/tmp/test.json'],
            ['--mode', 'status', '--endpoint', '192.0.2.10:3551'],
            ['--mode', 'host-gate'],
            ['--mode', 'service-gate'],
        ):
            with self.subTest(args=args):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        ups.main(args)
                self.assertEqual(raised.exception.code, 2)


    def test_invalid_grace_fails_open_for_both_gates(self):
        for invalid in (-1, 86401, "300", True, None):
            with self.subTest(grace=invalid):
                state = ups.advance(None, True, FIXED_NOW)
                state['grace'] = invalid
                self.assertEqual(ups.gate(state, FIXED_NOW, host=True)[0], 0)
                self.assertEqual(ups.gate(state, FIXED_NOW)[0], 3)

    def test_grace_change_preserves_existing_recovery_deadline(self):
        outage = ups.advance(None, True, FIXED_NOW)
        recovered = ups.advance(outage, False, FIXED_NOW + 10)
        deadline = recovered['recover_until']

        updated = ups.advance(
            recovered, False, FIXED_NOW + 20, grace=120
        )
        self.assertEqual(updated['recover_until'], deadline)
        self.assertEqual(ups.gate(updated, FIXED_NOW + 200)[0], 2)
        self.assertEqual(ups.gate(updated, deadline)[0], 0)


if __name__ == '__main__':
    unittest.main()
