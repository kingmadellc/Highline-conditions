#!/usr/bin/env python3
"""Freeze the active Satellite Beach morning; standard library, public NOAA only.

Keep the output directory durable, or use --restore-published on every clean CI
checkout. No game score service, secrets, scheduling or deployment is performed.
"""
import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
LOCATION = 'satellite-beach'
ZONE = ZoneInfo('America/New_York')
RULES = 'daily-042-v1'
REVISION = '0.42'
PUBLIC = 'https://kingmadellc.github.io/Highline-conditions/daily/'
MAX_BYTES = 48000
PACK_FIELDS = ('sourceKind', 'waveStation', 'waveUnixMinute', 'significantHeightMm',
               'dominantPeriodMs', 'waveFromDegrees', 'windStation', 'windUnixMinute',
               'windSpeedMmps', 'windFromDegrees', 'tideStation', 'tideUnixMinute',
               'waterLevelMm', 'tideChangeMm', 'provenanceKey')


def utc_text(when):
    return when.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_utc(value):
    when = dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)
    if utc_text(when) != value:
        raise ValueError('Noncanonical UTC timestamp')
    return when


def boundary(day):
    return dt.datetime.combine(day, dt.time(7), ZONE).astimezone(UTC)


def active_day(now):
    if now.tzinfo is None or now.utcoffset() != dt.timedelta(0):
        raise ValueError('Explicit UTC required')
    day = now.astimezone(ZONE).date()
    return day - dt.timedelta(days=1) if now < boundary(day) else day


def tide_url(cutoff):
    query = urllib.parse.urlencode(dict(
        begin_date=(cutoff-dt.timedelta(hours=3)).strftime('%Y%m%d %H:%M'),
        end_date=cutoff.strftime('%Y%m%d %H:%M'), station='8721604',
        product='water_level', datum='MLLW', time_zone='gmt', units='metric',
        format='json', application='Highline_Daily_Wave'))
    return 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?' + query


def importer(cutoff):
    # An isolated importer keeps the rolling feed and its URL contract unchanged.
    spec = importlib.util.spec_from_file_location('daily_observation_importer',
                                                Path(__file__).with_name('fetch-current.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TIDE = tide_url(cutoff)
    module.URLS = (module.NEAR, module.OFFSHORE, module.TIDE)
    return module


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def packed(snapshot):
    result = 'O1,' + ','.join(str(snapshot[key]) for key in PACK_FIELDS)
    if len(result) > 160:
        raise ValueError('Snapshot exceeds Unity packed bound')
    return result


def observed_seed(snapshot_packed):
    value = 2166136261
    for byte in snapshot_packed.encode('ascii'):
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value if value < 0x80000000 else value - 0x100000000


def layout(day, snapshot_packed):
    material = '\n'.join((LOCATION, day.isoformat(), RULES, REVISION, snapshot_packed))
    value = int(digest(material.encode())[:8], 16) % 999999
    # SurfWave.SequenceIndex(seed) is uint(seed) % 3. Rotating the authored
    # arrangement guarantees adjacent days differ even when weather repeats.
    sequence = day.toordinal() % 3
    seed = value - value % 3 + sequence
    return seed, 'surf-wave-sequence-v1:' + str(sequence)


def envelope(now):
    day = active_day(now)
    close = boundary(day + dt.timedelta(days=1))
    return dict(schemaVersion=1, status='unavailable', challengeId=LOCATION+'-'+day.isoformat(),
                locationId=LOCATION, localDate=day.isoformat(), locationTimeZone=ZONE.key,
                observationCutoffUtc=utc_text(boundary(day)), opensAtUtc=utc_text(boundary(day)),
                closesAtUtc=utc_text(close), submissionClosesAtUtc=utc_text(close+dt.timedelta(minutes=2)),
                publishedAtUtc=utc_text(now), rulesId=RULES, simulationRevision=REVISION)


def seal(document):
    result = copy.deepcopy(document)
    result.pop('manifestHash', None)
    result['manifestHash'] = digest(canonical(result))
    if len(canonical(result)) > MAX_BYTES:
        raise ValueError('Daily manifest exceeds bound')
    return result


def build(payloads, now):
    document = envelope(now)
    cutoff = parse_utc(document['observationCutoffUtc'])
    feed = importer(cutoff)
    try:
        observed = feed.build(payloads, feed.minute(cutoff))
    except ValueError:
        document['reason'] = 'morning_observations_unavailable'
        return seal(document)
    snapshot = observed['bundle']['snapshot']
    snapshot_packed = packed(snapshot)
    seed, signature = layout(dt.date.fromisoformat(document['localDate']), snapshot_packed)
    document.update(status='available', snapshotPacked=snapshot_packed,
                    snapshotHash=digest(snapshot_packed.encode()),
                    observedConditionSeed=observed_seed(snapshot_packed), waveSeed=seed,
                    layoutSignature=signature, observationBundle=observed['bundle'],
                    fixedSettings=dict(handling='Balanced', condition=0, startClock=0),
                    attemptLimits=dict(simulationSeconds=90, elapsedSeconds=600, submissionGraceSeconds=120),
                    rankedServiceAvailable=False)
    result = seal(document)
    validate(result)
    return result


def validate(document):
    if len(canonical(document)) > MAX_BYTES or document.get('schemaVersion') != 1:
        raise ValueError('Unsupported/unbounded daily manifest')
    hashless = copy.deepcopy(document)
    claimed = hashless.pop('manifestHash', None)
    if claimed != digest(canonical(hashless)):
        raise ValueError('Manifest hash mismatch')
    published = parse_utc(document['publishedAtUtc'])
    expected = envelope(published)
    for key in expected:
        if key != 'status' and document.get(key) != expected[key]:
            raise ValueError('Daily identity/boundary mismatch: '+key)
    if document['status'] == 'unavailable':
        if document.get('reason') != 'morning_observations_unavailable' or any(
                key in document for key in ('snapshotPacked', 'waveSeed', 'observationBundle')):
            raise ValueError('Unavailable daily wave must not contain a playable snapshot')
        return document
    if document['status'] != 'available':
        raise ValueError('Unknown daily status')
    cutoff = parse_utc(document['observationCutoffUtc'])
    feed = importer(cutoff)
    observation = dict(schema=1, generatedUnixMinute=feed.minute(cutoff), bundle=document['observationBundle'])
    # Fresh at the frozen cutoff, never rolling-current freshness at playback time.
    feed.validate(observation, feed.minute(cutoff))
    snapshot_packed = packed(observation['bundle']['snapshot'])
    seed, signature = layout(dt.date.fromisoformat(document['localDate']), snapshot_packed)
    for key, value in dict(snapshotPacked=snapshot_packed, snapshotHash=digest(snapshot_packed.encode()),
                           observedConditionSeed=observed_seed(snapshot_packed), waveSeed=seed,
                           layoutSignature=signature, rankedServiceAvailable=False,
                           fixedSettings=dict(handling='Balanced', condition=0, startClock=0),
                           attemptLimits=dict(simulationSeconds=90, elapsedSeconds=600, submissionGraceSeconds=120)).items():
        if document.get(key) != value:
            raise ValueError('Daily snapshot/rules mismatch: '+key)
    return document


def read_manifest(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Daily manifest exceeds bound')
    return validate(json.loads(data))


def install_immutable(path, data):
    """Publish complete bytes exclusively; concurrent writers read the winner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.daily-', delete=False) as handle:
            temporary = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        result = path.read_bytes()
        read_manifest(result)
        return result
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def restore_day(output, day, feed):
    path = output / (day.isoformat()+'.json')
    if path.exists():
        data = path.read_bytes()
    else:
        try:
            data = feed.fetch(PUBLIC+day.isoformat()+'.json')
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
    existing = read_manifest(data)
    if existing['status'] != 'available' or existing['localDate'] != day.isoformat():
        raise ValueError('Published daily identity mismatch')
    return install_immutable(path, data)


def publish(output, now, payloads=None, restore_published=False, raw_dir=None):
    output = Path(output)
    day = active_day(now)
    cutoff = boundary(day)
    feed = importer(cutoff)
    dated = output / (day.isoformat()+'.json')
    if restore_published:
        # The Pages feed retains today's and yesterday's dated files. Longer
        # history belongs to the score service's pinned manifest records.
        restore_day(output, day-dt.timedelta(days=1), feed)
    if dated.exists():
        data = dated.read_bytes()
    else:
        data = None
        if restore_published:
            # HTTP failures other than a confirmed 404 fail closed: rebuilding on
            # a transient outage could replace an already-published wave.
            data = restore_day(output, day, feed)
        if data is None:
            if payloads is None:
                payloads = {}
                for url in feed.URLS:
                    try:
                        payloads[url] = feed.fetch(url)
                    except (OSError, ValueError) as error:
                        print('Unavailable source:', url, str(error))
            if raw_dir:
                directory = Path(raw_dir)
                directory.mkdir(parents=True, exist_ok=True)
                receipts = []
                for url, payload in payloads.items():
                    sha = digest(payload)
                    (directory/(sha+'.raw')).write_bytes(payload)
                    receipts.append(dict(url=url, rawSha256=sha))
                feed.atomic_write(directory/(day.isoformat()+'-sources.json'), canonical(receipts)+b'\n')
            document = build(payloads, now)
            data = canonical(document)+b'\n'
            if document['status'] == 'available':
                previous = output / ((day-dt.timedelta(days=1)).isoformat()+'.json')
                if previous.exists() and read_manifest(previous.read_bytes()).get('layoutSignature') == document['layoutSignature']:
                    raise ValueError('Daily arrangement repeats yesterday; hold publication')
                data = install_immutable(dated, data)
    document = read_manifest(data)
    if document['localDate'] != day.isoformat():
        raise ValueError('Existing daily identity mismatch')
    feed.atomic_write(output/'current.json', data)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('site/daily'))
    parser.add_argument('--restore-published', action='store_true')
    parser.add_argument('--raw-dir', type=Path)
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    if args.validate:
        document = read_manifest(args.validate.read_bytes())
    else:
        document = publish(args.output_dir, dt.datetime.now(UTC),
                           restore_published=args.restore_published, raw_dir=args.raw_dir)
    print(json.dumps({key:document[key] for key in ('status', 'challengeId', 'opensAtUtc', 'closesAtUtc', 'manifestHash')}))


if __name__ == '__main__':
    main()
