#!/usr/bin/env python3
"""Bounded public NOAA observation feed. Standard library only; never reads game files.
Failed/stale fetches exit before replacement so an existing Pages deployment remains intact.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.request

MISSING = -99999
MAX_BYTES = 32000
NEAR = 'https://www.ndbc.noaa.gov/data/realtime2/41113.txt'
OFFSHORE = 'https://www.ndbc.noaa.gov/data/realtime2/41009.txt'
TIDE = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?range=3&station=8721604&product=water_level&datum=MLLW&time_zone=gmt&units=metric&format=json&application=Highline_Observations'
URLS = (NEAR, OFFSHORE, TIDE)
WAVE_UNITS = 'Hs m; dominant period s; direction FROM degrees true'
WIND_UNITS = 'm/s; direction FROM degrees true'
TIDE_UNITS = 'm relative to MLLW at Trident Pier'
NDBC_HEADER = '#YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP DEWP VIS TIDE\n#yr mo dy hr mn degT m/s m/s m sec sec degT hPa degC degC degC mi ft\n'


def minute(when):
    if when.tzinfo is None or when.utcoffset() != dt.timedelta(0):
        raise ValueError('Explicit UTC required')
    return int(when.timestamp() // 60)


def utc(value):
    return dt.datetime.fromtimestamp(value*60, dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def number(value, sentinel, low, high, scale=1):
    try:
        n = float(value)
    except (ValueError, TypeError):
        return MISSING
    if not math.isfinite(n) or n == sentinel or not low <= n <= high:
        return MISSING
    return int(math.floor(n*scale+.5)) if n >= 0 else -int(math.floor(-n*scale+.5))


def ndbc(payload):
    lines = payload.decode('ascii').splitlines()
    if len(lines) < 3 or not lines[0].startswith('#YY') or not lines[1].startswith('#yr'):
        raise ValueError('NOAA UTC header missing')
    keys, units = lines[0].lstrip('#').split(), lines[1].lstrip('#').split()
    if len(keys) != len(units) or keys[:5] != ['YY', 'MM', 'DD', 'hh', 'mm'] or units[:5] != ['yr', 'mo', 'dy', 'hr', 'mn']:
        raise ValueError('NOAA column/time units changed')
    for key, unit in {'WVHT':'m','DPD':'sec','MWD':'degT','WDIR':'degT','WSPD':'m/s'}.items():
        if key not in keys or units[keys.index(key)] != unit:
            raise ValueError('NOAA units changed: '+key)
    rows = []
    for line in lines[2:]:
        f = line.split()
        if len(f) != len(keys):
            continue
        try:
            when = dt.datetime(*map(int, f[:5]), tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if not 2000 <= when.year <= 2100:
            continue
        row = dict(zip(keys, f))
        wd, windd = number(row['MWD'],999,0,360), number(row['WDIR'],999,0,360)
        rows.append(dict(minute=minute(when), raw=line,
                         height=number(row['WVHT'],99,0,30,1000), period=number(row['DPD'],99,.1,40,1000),
                         direction=0 if wd == 360 else wd, wind=number(row['WSPD'],99,0,80,1000),
                         windDirection=0 if windd == 360 else windd))
    return rows


def tide_rows(payload):
    data = json.loads(payload)
    if data.get('metadata',{}).get('id') != '8721604' or 'error' in data:
        raise ValueError('Wrong/unavailable NOAA water-level station')
    rows = {}
    for row in data.get('data',[]):
        if row.get('f') != '0,0,0,0' or row.get('q') not in ('p','v'):
            continue
        try:
            when = dt.datetime.strptime(row['t'], '%Y-%m-%d %H:%M').replace(tzinfo=dt.timezone.utc)
        except (ValueError, KeyError):
            continue
        level = number(row.get('v'),None,-5,10,1000)
        if level != MISSING and 2000 <= when.year <= 2100:
            rows[minute(when)] = dict(minute=minute(when), level=level, raw={k:row[k] for k in ('t','v','f','q')})
    return sorted(rows.values(),key=lambda r:r['minute'])


def choose(rows, target, limit, fields):
    return max((r for r in rows if 0 <= target-r['minute'] <= limit and all(r[f] != MISSING for f in fields)),
               key=lambda r:r['minute'],default=None)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def provenance(sources):
    return digest('\n'.join(s['url']+'\n'+s['rawSha256'] for s in sources).encode())[:24]


def build(payloads, now):
    parsed = {}
    for url, payload in payloads.items():
        if url not in URLS or not isinstance(payload,bytes) or len(payload)>4000000:
            raise ValueError('Unrecognized or unbounded source')
        try:
            parsed[url] = tide_rows(payload) if url == TIDE else ndbc(payload)
        except (ValueError, IndexError, KeyError, UnicodeDecodeError, AttributeError, TypeError):
            parsed[url] = []
    wave_url = NEAR
    wave = choose(parsed.get(NEAR,[]),now,120,('height','period'))
    if wave is None:
        wave_url = OFFSHORE
        wave = choose(parsed.get(OFFSHORE,[]),now,120,('height','period'))
    if wave is None:
        raise ValueError('No valid official wave observation within 120 minutes; keep previous deployment')
    wind = choose(parsed.get(OFFSHORE,[]),now,90,('wind','windDirection'))
    if wind is not None and abs(wind['minute']-wave['minute'])>120:
        wind = None
    tide = choose(parsed.get(TIDE,[]),now,30,('level',))
    if tide is not None and abs(tide['minute']-wave['minute'])>120:
        tide = None
    previous = next((r for r in parsed.get(TIDE,[]) if tide and r['minute'] == tide['minute']-6),None)
    delta = tide['level']-previous['level'] if previous else MISSING
    if delta != MISSING and abs(delta)>500:
        previous, delta = None, MISSING
    source_urls = [wave_url]
    if wind and OFFSHORE not in source_urls:
        source_urls.append(OFFSHORE)
    if tide:
        source_urls.append(TIDE)
    sources = [dict(url=u,rawSha256=digest(payloads[u]),payloadSha256=digest(payloads[u])) for u in source_urls]
    s = dict(revision=1,sourceKind=2,waveStation=41113 if wave_url==NEAR else 41009,waveUnixMinute=wave['minute'],
             significantHeightMm=wave['height'],dominantPeriodMs=wave['period'],waveFromDegrees=wave['direction'],
             windStation=41009 if wind else 0,windUnixMinute=wind['minute'] if wind else 0,
             windSpeedMmps=wind['wind'] if wind else MISSING,windFromDegrees=wind['windDirection'] if wind else MISSING,
             tideStation=8721604 if tide else 0,tideUnixMinute=tide['minute'] if tide else 0,
             waterLevelMm=tide['level'] if tide else MISSING,tideChangeMm=delta,provenanceKey=provenance(sources))
    bundle = dict(id='current-'+s['provenanceKey'],snapshot=s,sources=sources,
                  waveHeader=payloads[wave_url].decode('ascii').splitlines()[0],waveHeaderUnits=payloads[wave_url].decode('ascii').splitlines()[1],
                  windHeader=payloads[OFFSHORE].decode('ascii').splitlines()[0] if wind else '',windHeaderUnits=payloads[OFFSHORE].decode('ascii').splitlines()[1] if wind else '',
                  selectedWaveRow=wave['raw'],selectedWindRow=wind['raw'] if wind else '',
                  selectedTideRows=[json.dumps(r['raw'],separators=(',',':')) for r in ([previous,tide] if previous else [tide]) if r],
                  waterLevelSeries=[dict(unixMinute=r['minute'],waterLevelMm=r['level'],quality=1 if r['raw']['q']=='v' else 2)
                                    for r in parsed.get(TIDE,[]) if tide and 0<=now-r['minute']<=180][-31:],
                  waterLevelSeriesScope='Recent observed levels only (requested 3 hours); gaps retained; UTC; station 8721604; m MLLW',
                  timeBasis='UTC',waveUnits=WAVE_UNITS,windUnits=WIND_UNITS,waterLevelUnits=TIDE_UNITS,
                  scope='Regional observations interpreted by an authored gameplay transform; not measured Hightower breaking faces.')
    document = dict(schema=1,generatedUnixMinute=now,bundle=bundle)
    validate(document,now)
    return document


def validate(document, now):
    if len(json.dumps(document,separators=(',',':')).encode())>MAX_BYTES:
        raise ValueError('Feed exceeds 32000 bytes')
    if set(document)!= {'schema','generatedUnixMinute','bundle'} or document['schema']!=1:
        raise ValueError('Unsupported delivery schema')
    generated=document['generatedUnixMinute']
    if type(generated)!=int or not 0<=now-generated<=1440:
        raise ValueError('Invalid/future/expired generation time')
    b=document['bundle'];s=b['snapshot'];sources=b['sources']
    if (s['sourceKind']!=2 or s['revision']!=1 or b['timeBasis']!='UTC' or b['waveUnits']!=WAVE_UNITS
            or b['windUnits']!=WIND_UNITS or b['waterLevelUnits']!=TIDE_UNITS):
        raise ValueError('Invalid origin or declared units')
    wave_url=NEAR if s['waveStation']==41113 else OFFSHORE if s['waveStation']==41009 else None
    expected=[wave_url]
    if s['windStation']==41009 and OFFSHORE not in expected:expected.append(OFFSHORE)
    if s['tideStation']==8721604:expected.append(TIDE)
    if [r['url'] for r in sources]!=expected or not wave_url:
        raise ValueError('Wrong/duplicate source station URL')
    for source in sources:
        if set(source)!={'url','rawSha256','payloadSha256'} or source['rawSha256']!=source['payloadSha256'] or len(source['rawSha256'])!=64 or any(c not in '0123456789abcdef' for c in source['rawSha256']):
            raise ValueError('Invalid public source receipt')
    if provenance(sources)!=s['provenanceKey'] or b['id']!='current-'+s['provenanceKey']:
        raise ValueError('Provenance mismatch')
    wave=choose(ndbc(('\n'.join((b['waveHeader'],b['waveHeaderUnits'],b['selectedWaveRow']))).encode()),generated,120,('height','period'))
    if not wave or any(s[k]!=wave[v] for k,v in {'waveUnixMinute':'minute','significantHeightMm':'height','dominantPeriodMs':'period','waveFromDegrees':'direction'}.items()):
        raise ValueError('Wave snapshot differs from original NOAA row')
    if not 0<=now-s['waveUnixMinute']<=120:
        raise ValueError('Wave timestamp stale/future')
    if s['windStation']==41009:
        wind=choose(ndbc(('\n'.join((b['windHeader'],b['windHeaderUnits'],b['selectedWindRow']))).encode()),generated,90,('wind','windDirection'))
        if not wind or not 0<=now-wind['minute']<=90 or abs(wave['minute']-wind['minute'])>120 or any(s[k]!=wind[v] for k,v in {'windUnixMinute':'minute','windSpeedMmps':'wind','windFromDegrees':'windDirection'}.items()):
            raise ValueError('Invalid/stale/future wind')
    elif s['windStation']!=0 or s['windUnixMinute']!=0 or s['windSpeedMmps']!=MISSING or s['windFromDegrees']!=MISSING or b['selectedWindRow']:
        raise ValueError('Missing wind must remain missing')
    rows=tide_rows(json.dumps(dict(metadata=dict(id='8721604'),data=[json.loads(r) for r in b['selectedTideRows']])).encode())
    if s['tideStation']==8721604:
        tide=choose(rows,generated,30,('level',))
        prev=next((r for r in rows if tide and r['minute']==tide['minute']-6),None)
        delta=tide['level']-prev['level'] if prev else MISSING
        if not tide or not 0<=now-tide['minute']<=30 or abs(wave['minute']-tide['minute'])>120 or s['tideUnixMinute']!=tide['minute'] or s['waterLevelMm']!=tide['level'] or s['tideChangeMm']!=delta or delta!=MISSING and abs(delta)>500:
            raise ValueError('Invalid/stale/future water level or QC')
    elif s['tideStation']!=0 or s['tideUnixMinute']!=0 or s['waterLevelMm']!=MISSING or s['tideChangeMm']!=MISSING or rows or b['waterLevelSeries']:
        raise ValueError('Missing tide must remain missing')
    series=b['waterLevelSeries']
    if len(series)>31 or any(type(p['unixMinute'])!=int or not 0<=generated-p['unixMinute']<=180 or not -5000<=p['waterLevelMm']<=10000 or p['quality'] not in (1,2) for p in series) or any(a['unixMinute']>=z['unixMinute'] for a,z in zip(series,series[1:])):
        raise ValueError('Invalid bounded tide series')
    return document


def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'Highline-Public-Observations/1.0'})
    with urllib.request.urlopen(request,timeout=20) as response:
        if response.geturl()!=url or response.status!=200:
            raise ValueError('Unexpected source location/status')
        data=response.read(4000001)
        if len(data)>4000000:raise ValueError('Source exceeds bound')
        return data


def atomic_write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.observation-',delete=False) as f:
            temporary=f.name;f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary and os.path.exists(temporary):os.unlink(temporary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('site/current.json'))
    parser.add_argument('--validate',type=Path)
    args=parser.parse_args()
    if args.validate:
        data=args.validate.read_bytes()
        if len(data)>MAX_BYTES:raise ValueError('Feed exceeds bound')
        validate(json.loads(data),minute(dt.datetime.now(dt.timezone.utc)))
        print('PASS: public snapshot sources, QC, timestamps, units and bounds')
        return
    payloads={}
    for url in URLS:
        try:payloads[url]=fetch(url)
        except (OSError,ValueError) as error:print('Unavailable source:',url,str(error))
    document=build(payloads,minute(dt.datetime.now(dt.timezone.utc)))
    encoded=(json.dumps(document,separators=(',',':'))+'\n').encode()
    if len(encoded)>MAX_BYTES:raise ValueError('Feed exceeds bound')
    atomic_write(args.output,encoded)
    s=document['bundle']['snapshot']
    print(json.dumps(dict(status='fresh',bytes=len(encoded),generated=utc(document['generatedUnixMinute']),
                         waveStation=s['waveStation'],wave=utc(s['waveUnixMinute']),
                         wind=utc(s['windUnixMinute']) if s['windStation'] else 'unavailable',
                         waterLevel=utc(s['tideUnixMinute']) if s['tideStation'] else 'unavailable')))


if __name__=='__main__':main()
