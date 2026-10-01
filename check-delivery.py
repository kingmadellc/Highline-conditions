#!/usr/bin/env python3
"""Mechanical delivery failure cases, with synthetic parser fixtures, no game/assets."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('feed',Path(__file__).with_name('fetch-current.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
NOW=f.minute(dt.datetime(2026,10,1,5,30,tzinfo=dt.timezone.utc))
HEADER=f.NDBC_HEADER
WAVE='2026 10 01 05 26 MM MM MM 0.50 13.00 5.20 93 MM MM 27 MM MM MM'
WIND='2026 10 01 05 20 90 5.0 6.0 1.10 12.00 5.50 95 1010 27 27 24 MM MM'
TIDE={'metadata':{'id':'8721604'},'data':[
    {'t':'2026-10-01 05:18','v':'0.930','f':'0,0,0,0','q':'p'},
    {'t':'2026-10-01 05:24','v':'0.933','f':'0,0,0,0','q':'p'},
    {'t':'2026-10-01 05:30','v':'1.999','f':'1,0,0,0','q':'p'}]}

def payloads():return {f.NEAR:(HEADER+WAVE+'\n').encode(),f.OFFSHORE:(HEADER+WIND+'\n').encode(),f.TIDE:json.dumps(TIDE).encode()}

class Checks(unittest.TestCase):
    def test_complete_fresh_source_times_units_and_quality(self):
        d=f.build(payloads(),NOW);s=d['bundle']['snapshot']
        self.assertEqual((s['waveUnixMinute'],s['windUnixMinute'],s['tideUnixMinute']),(NOW-4,NOW-10,NOW-6))
        self.assertEqual((s['significantHeightMm'],s['dominantPeriodMs'],s['windSpeedMmps'],s['waterLevelMm'],s['tideChangeMm']),(500,13000,5000,933,3))
        self.assertEqual(len(d['bundle']['waterLevelSeries']),2)
        self.assertLess(len(json.dumps(d).encode()),f.MAX_BYTES)
    def test_offshore_fallback_identified(self):
        p=payloads();del p[f.NEAR];s=f.build(p,NOW)['bundle']['snapshot']
        self.assertEqual(s['waveStation'],41009)
    def test_partial_missing_values_are_not_zero(self):
        d=f.build({f.NEAR:payloads()[f.NEAR]},NOW);s=d['bundle']['snapshot']
        self.assertEqual((s['windStation'],s['tideStation']),(0,0))
        self.assertEqual((s['windSpeedMmps'],s['waterLevelMm']),(f.MISSING,f.MISSING))
    def test_fresh_tide_and_wind_may_follow_slower_wave_station(self):
        p=payloads();p[f.OFFSHORE]=p[f.OFFSHORE].replace(b'05 20',b'05 30')
        t=copy.deepcopy(TIDE);t['data'][-1]['f']='0,0,0,0';p[f.TIDE]=json.dumps(t).encode()
        s=f.build(p,NOW)['bundle']['snapshot']
        self.assertEqual((s['waveUnixMinute'],s['windUnixMinute'],s['tideUnixMinute']),(NOW-4,NOW,NOW))
        self.assertEqual(s['waterLevelMm'],1999)
    def test_wrong_units_and_wrong_station_rejected(self):
        p=payloads();p[f.NEAR]=p[f.NEAR].replace(b'm sec sec',b'ft sec sec');p.pop(f.OFFSHORE)
        with self.assertRaises(ValueError):f.build(p,NOW)
        p=payloads();p[f.TIDE]=p[f.TIDE].replace(b'8721604',b'1234567')
        self.assertEqual(f.build(p,NOW)['bundle']['snapshot']['tideStation'],0)
    def test_future_or_stale_wave_is_not_retimestamped(self):
        for now in (NOW-11,NOW+121):
            with self.assertRaises(ValueError):f.build(payloads(),now)
    def test_malformed_and_missing_provider_bodies(self):
        for p in ({},{f.NEAR:b'error'},{f.NEAR:b'X'*4000001},{'https://example.org/41113.txt':b'error'}):
            with self.assertRaises(ValueError):f.build(p,NOW)
    def test_malformed_optional_tide_keeps_valid_waves(self):
        for body in (b'[]',b'null',b'{"metadata":{"id":"8721604"},"data":[null]}'):
            p=payloads();p[f.TIDE]=body
            self.assertEqual(f.build(p,NOW)['bundle']['snapshot']['tideStation'],0)
    def test_missing_sentinels_calm_north_zero(self):
        for n in ('MM','99.0','nan','Infinity','-1'):self.assertEqual(f.number(n,99,0,30,1000),f.MISSING)
        self.assertEqual(f.number('0',99,0,30,1000),0)
    def test_corrupt_receipts_rows_and_future_generation_rejected(self):
        for mutate in (lambda d:d.update(generatedUnixMinute=NOW+1),lambda d:d['bundle']['sources'][0].update(url=f.OFFSHORE),lambda d:d['bundle']['snapshot'].update(significantHeightMm=501),lambda d:d['bundle'].update(timeBasis='local'),lambda d:d['bundle']['sources'][0].update(rawSha256='g'*64)):
            d=f.build(payloads(),NOW);mutate(d)
            with self.assertRaises(ValueError):f.validate(d,NOW)
    def test_stale_component_removed_before_publish(self):
        d=f.build(payloads(),NOW+30)['bundle']['snapshot']
        self.assertEqual(d['tideStation'],0);self.assertEqual(d['waveUnixMinute'],NOW-4)
    def test_current_ndbc_extra_pressure_column(self):
        p=payloads()
        for u in (f.NEAR,f.OFFSHORE):
            lines=p[u].decode().splitlines();lines[0]=lines[0].replace('VIS TIDE','VIS PTDY TIDE');lines[1]=lines[1].replace('mi ft','nmi hPa ft');lines[2]+=' MM';p[u]=('\n'.join(lines)+'\n').encode()
        self.assertEqual(f.build(p,NOW)['bundle']['snapshot']['significantHeightMm'],500)
    def test_failure_preserves_last_valid_file(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'current.json';prior=json.dumps(f.build(payloads(),NOW)).encode();f.atomic_write(p,prior)
            with self.assertRaises(ValueError):
                d=f.build({},NOW+1500);f.atomic_write(p,json.dumps(d).encode())
            self.assertEqual(p.read_bytes(),prior)
    def test_flagged_tide_never_becomes_verified(self):
        p=payloads();t=copy.deepcopy(TIDE)
        for r in t['data']:r['f']='1,0,0,0'
        p[f.TIDE]=json.dumps(t).encode()
        self.assertEqual(f.build(p,NOW)['bundle']['snapshot']['tideStation'],0)

if __name__=='__main__':unittest.main()
