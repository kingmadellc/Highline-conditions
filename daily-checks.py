#!/usr/bin/env python3
"""Offline daily publisher gates; synthetic rows are never production fixtures."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('daily', Path(__file__).with_name('daily-publisher.py'))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
NOW = dt.datetime(2026, 10, 6, 18, 30, tzinfo=d.UTC)
CUTOFF = d.boundary(d.active_day(NOW))


def fixture(now=NOW):
    cutoff = d.boundary(d.active_day(now))
    f = d.importer(cutoff)
    def row(when, rest):
        return when.strftime('%Y %m %d %H %M ') + rest + '\n'
    near = row(cutoff-dt.timedelta(minutes=4), 'MM MM MM 0.50 13.00 5.20 93 MM MM 27 MM MM MM')
    offshore = row(cutoff-dt.timedelta(minutes=10), '90 5.0 6.0 1.10 12.00 5.50 95 1010 27 27 24 MM MM')
    tide = dict(metadata=dict(id='8721604'), data=[
        dict(t=(cutoff-dt.timedelta(minutes=12)).strftime('%Y-%m-%d %H:%M'), v='0.930', f='0,0,0,0', q='p'),
        dict(t=(cutoff-dt.timedelta(minutes=6)).strftime('%Y-%m-%d %H:%M'), v='0.933', f='0,0,0,0', q='p')])
    return {f.NEAR:(f.NDBC_HEADER+near).encode(), f.OFFSHORE:(f.NDBC_HEADER+offshore).encode(),
            f.TIDE:json.dumps(tide).encode()}


class DailyChecks(unittest.TestCase):
    def test_spring_day_is_23_hours(self):
        day = dt.date(2026, 3, 7)
        self.assertEqual(d.boundary(day+dt.timedelta(days=1))-d.boundary(day), dt.timedelta(hours=23))

    def test_fall_day_is_25_hours(self):
        day = dt.date(2026, 10, 31)
        self.assertEqual(d.boundary(day+dt.timedelta(days=1))-d.boundary(day), dt.timedelta(hours=25))

    def test_before_seven_uses_previous_day_everywhere(self):
        self.assertEqual(d.active_day(CUTOFF-dt.timedelta(seconds=1)), dt.date(2026, 10, 5))
        self.assertEqual(d.active_day(CUTOFF), dt.date(2026, 10, 6))
        with self.assertRaises(ValueError):
            d.active_day(dt.datetime(2026, 10, 6, 7))

    def test_delayed_fetch_uses_morning_not_afternoon(self):
        p = fixture()
        f = d.importer(CUTOFF)
        afternoon = NOW.strftime('%Y %m %d %H %M ')+'MM MM MM 9.99 20.00 5.20 93 MM MM 27 MM MM MM\n'
        p[f.NEAR] += afternoon.encode()
        doc = d.build(p, NOW)
        self.assertEqual(doc['status'], 'available')
        self.assertEqual(doc['observationBundle']['snapshot']['significantHeightMm'], 500)
        self.assertEqual(doc['opensAtUtc'], '2026-10-06T11:00:00Z')
        self.assertEqual(doc['closesAtUtc'], '2026-10-07T11:00:00Z')
        d.validate(doc)  # Still valid seven hours after rolling feed freshness expires.

    def test_afternoon_only_does_not_manufacture_morning(self):
        f = d.importer(CUTOFF)
        p = {f.NEAR:fixture()[f.NEAR].replace(b'10 56', b'18 26')}
        self.assertEqual(d.build(p, NOW)['status'], 'unavailable')

    def test_missing_and_stale_required_waves_are_unavailable(self):
        f = d.importer(CUTOFF)
        for p in ({}, {f.NEAR:fixture()[f.NEAR].replace(b'10 56', b'08 56')}):
            doc = d.build(p, NOW)
            self.assertEqual(doc['status'], 'unavailable')
            self.assertNotIn('snapshotPacked', doc)
            self.assertNotIn('waveSeed', doc)
            d.validate(doc)

    def test_offshore_wave_fallback_keeps_provenance(self):
        p = fixture()
        f = d.importer(CUTOFF)
        del p[f.NEAR]
        self.assertEqual(d.build(p, NOW)['observationBundle']['snapshot']['waveStation'], 41009)

    def test_missing_optional_sources_are_explicit(self):
        f = d.importer(CUTOFF)
        s = d.build({f.NEAR:fixture()[f.NEAR]}, NOW)['observationBundle']['snapshot']
        self.assertEqual((s['windStation'], s['tideStation']), (0, 0))
        self.assertEqual((s['windSpeedMmps'], s['waterLevelMm']), (-99999, -99999))

    def test_future_and_flagged_optional_rows_stay_missing(self):
        f = d.importer(CUTOFF)
        p = fixture()
        p[f.OFFSHORE] = p[f.OFFSHORE].replace(b'10 50', b'11 01')
        p[f.TIDE] = p[f.TIDE].replace(b'0,0,0,0', b'1,0,0,0')
        s = d.build(p, NOW)['observationBundle']['snapshot']
        self.assertEqual((s['windStation'], s['tideStation']), (0, 0))

    def test_actual_tide_request_and_raw_hash_retained(self):
        doc = d.build(fixture(), NOW)
        source = doc['observationBundle']['sources'][-1]
        self.assertIn('begin_date=20261006+08%3A00', source['url'])
        self.assertIn('end_date=20261006+11%3A00', source['url'])
        self.assertEqual(source['rawSha256'], d.digest(fixture()[source['url']]))

    def test_unchanged_snapshot_has_different_adjacent_date_layout(self):
        text = d.build(fixture(), NOW)['snapshotPacked']
        first, first_signature = d.layout(dt.date(2026, 10, 6), text)
        second, second_signature = d.layout(dt.date(2026, 10, 7), text)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first_signature, second_signature)
        self.assertNotEqual(first % 3, second % 3)
        self.assertEqual(d.layout(dt.date(2026, 10, 6), text), (first, first_signature))

    def test_packed_contract_and_observed_seed_are_independent(self):
        doc = d.build(fixture(), NOW)
        self.assertTrue(doc['snapshotPacked'].startswith('O1,2,41113,'))
        self.assertEqual(len(doc['snapshotPacked'].split(',')), 16)
        self.assertLessEqual(len(doc['snapshotPacked']), 160)
        self.assertEqual(d.observed_seed('hello'), 1335831723)
        self.assertEqual(doc['observedConditionSeed'], d.observed_seed(doc['snapshotPacked']))
        self.assertNotEqual(doc['observedConditionSeed'], doc['waveSeed'])

    def test_changed_rows_seed_boundaries_hash_and_rules_rejected(self):
        original = d.build(fixture(), NOW)
        for mutate in (
                lambda x:x.update(waveSeed=x['waveSeed']+1),
                lambda x:x.update(closesAtUtc='2026-10-07T12:00:00Z'),
                lambda x:x.update(rulesId='different'),
                lambda x:x.update(snapshotPacked=x['snapshotPacked']+'0'),
                lambda x:x['observationBundle']['snapshot'].update(significantHeightMm=999),
                lambda x:x['fixedSettings'].update(handling='Assisted')):
            bad = copy.deepcopy(original)
            mutate(bad)
            with self.assertRaises(ValueError):
                d.validate(d.seal(bad))
        original['manifestHash'] = '0'*64
        with self.assertRaises(ValueError):
            d.validate(original)

    def test_published_file_and_current_bytes_never_change_for_day(self):
        with tempfile.TemporaryDirectory() as temp:
            first = d.publish(temp, NOW, fixture())
            dated = Path(temp)/'2026-10-06.json'
            raw = dated.read_bytes()
            second = d.publish(temp, NOW+dt.timedelta(hours=2), {})
            self.assertEqual(first, second)
            self.assertEqual(dated.read_bytes(), raw)
            self.assertEqual((Path(temp)/'current.json').read_bytes(), raw)

    def test_unavailable_does_not_permanently_freeze_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(d.publish(temp, NOW, {})['status'], 'unavailable')
            self.assertFalse((Path(temp)/'2026-10-06.json').exists())
            self.assertEqual(d.publish(temp, NOW+dt.timedelta(minutes=2), fixture())['status'], 'available')

    def test_exclusive_creation_returns_first_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'2026-10-06.json'
            original = d.canonical(d.build(fixture(), NOW))+b'\n'
            changed = d.canonical(d.build(fixture(), NOW+dt.timedelta(seconds=1)))+b'\n'
            self.assertEqual(d.install_immutable(path, original), original)
            self.assertEqual(d.install_immutable(path, changed), original)

    def test_clean_checkout_restores_published_bytes(self):
        feed = d.importer(CUTOFF)
        raw = d.canonical(d.build(fixture(), NOW))+b'\n'
        yesterday = NOW-dt.timedelta(days=1)
        previous = d.canonical(d.build(fixture(yesterday), yesterday))+b'\n'
        def fetch_body(url):
            return previous if url.endswith('2026-10-05.json') else raw
        original_importer = d.importer
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'importer', side_effect=lambda c:feed if c == CUTOFF else original_importer(c)):
            with patch.object(feed, 'fetch', side_effect=fetch_body) as fetch:
                doc = d.publish(temp, NOW+dt.timedelta(hours=1), restore_published=True)
                self.assertEqual(doc['publishedAtUtc'], d.utc_text(NOW))
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual((Path(temp)/'2026-10-05.json').read_bytes(), previous)
                self.assertEqual((Path(temp)/'current.json').read_bytes(), raw)

    def test_restore_network_failure_does_not_rebuild(self):
        feed = d.importer(CUTOFF)
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'importer', return_value=feed):
            with patch.object(feed, 'fetch', side_effect=OSError('offline')):
                with self.assertRaises(OSError):
                    d.publish(temp, NOW, fixture(), restore_published=True)
            self.assertFalse((Path(temp)/'current.json').exists())

    def test_restore_confirmed_404_allows_first_publication(self):
        feed = d.importer(CUTOFF)
        with tempfile.TemporaryDirectory() as temp, patch.object(d, 'importer', return_value=feed):
            with patch.object(feed, 'fetch', side_effect=urllib.error.HTTPError('url', 404, 'missing', None, None)):
                self.assertEqual(d.publish(temp, NOW, fixture(), restore_published=True)['status'], 'available')

    def test_day_rollover_keeps_prior_manifest_and_new_identity(self):
        tomorrow = NOW+dt.timedelta(days=1)
        with tempfile.TemporaryDirectory() as temp:
            first = d.publish(temp, NOW, fixture())
            second = d.publish(temp, tomorrow, fixture(tomorrow))
            self.assertNotEqual(first['challengeId'], second['challengeId'])
            self.assertNotEqual(first['layoutSignature'], second['layoutSignature'])
            self.assertTrue((Path(temp)/'2026-10-06.json').exists())
            self.assertTrue((Path(temp)/'2026-10-07.json').exists())


if __name__ == '__main__':
    unittest.main()
