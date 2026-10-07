# Frozen morning delivery

`daily-publisher.py` adds the daily wave alongside the rolling Free Surf feed. It uses only the standard library and public NOAA data. It does not deploy, schedule a job, store player data or validate scores.

```sh
python3 daily-checks.py
python3 daily-publisher.py --output-dir site/daily --raw-dir daily-source-receipts
python3 daily-publisher.py --validate site/daily/current.json
```

Keep `site/daily` durable. In a clean checkout, use `--restore-published` to retrieve that day's and the preceding day's previously published `https://kingmadellc.github.io/Highline-conditions/daily/YYYY-MM-DD.json` before creating anything. Only HTTP 404 allows a new freeze; a network error, malformed response or changed identity stops the run. It never replaces an existing dated file. The atomic, exclusive creation also ensures concurrent local publishers use the same winning bytes. `current.json` is an exact copy of the active dated manifest.

An unavailable attempt writes only `current.json` with `status: unavailable` and `reason: morning_observations_unavailable`. It has no playable snapshot or seed, and no dated immutable file. A later successful fetch can still publish eligible morning observations, with the original scheduled closing time. On rollover, an unavailable new day cannot expose yesterday's wave as today's ranked challenge.

## Morning cutoff and provenance

The challenge date is Satellite Beach's date at 07:00 America/New_York, or the preceding date before 07:00. Closing time is the next local 07:00, including 23-hour and 25-hour daylight-saving days. Publication can happen later; only observation timestamps at or before that day's cutoff are eligible.

NDBC's realtime station files retain rows from earlier that morning, so delayed retrieval can select the original observations. NOAA water levels use an explicit three-hour UTC `begin_date` / `end_date` window ending at the cutoff, with `product=water_level`, metric units, MLLW and the existing QC gates. Afternoon rows are never retimestamped. Required waves must be within 120 minutes of the cutoff; optional wind 90 minutes and optional tide 30 minutes, with the existing cross-component agreement bounds. Optional failure remains missing. No synthetic or dated archive fallback becomes an observed daily challenge.

The isolated existing importer validates the actual dated NOAA request URL. `observationBundle` retains the original selected rows, headers, UTC times, quality flags, station identities, metric units and SHA-256 hashes of the exact downloaded bodies. `--raw-dir` retains the corresponding raw bytes by hash outside the publication artifact. Hashes identify downloaded data; they are not provider signatures. Fetching a historical morning after the cutoff is explicitly represented by its later `publishedAtUtc`; it is not a claim that those bytes were retrieved at 07:00.

## Consumer contract

The manifest uses `schemaVersion: 1`, `locationId: satellite-beach`, and `challengeId: satellite-beach-YYYY-MM-DD`. Its ISO UTC timestamps are `observationCutoffUtc`, `opensAtUtc`, `closesAtUtc`, `submissionClosesAtUtc`, and `publishedAtUtc`. `localDate` and `locationTimeZone` retain the competition date and zone. Availability is explicit in `status`.

Available manifests add:

- `snapshotPacked`: exact canonical `O1` representation accepted by `SurfObservedConditions.TryUnpack(manifest.snapshotPacked, out SurfObservedSnapshot snapshot)`.
- `observedConditionSeed`: signed FNV-1a over the packed snapshot, matching `SurfObservedConditions.TryCreateSession(snapshot, out SurfSessionConditions conditions)`. Do not overwrite `conditions.seed` with the layout seed.
- `waveSeed`: separate bounded seed derived from location, date, rules, simulation revision and packed snapshot. Pass it as the game's wave seed.
- `layoutSignature`: `surf-wave-sequence-v1:0`, `:1` or `:2`, matching the existing `SurfWave.SequenceIndex(seed)`. The authored arrangement rotates with the date, so consecutive days have different arrangement signatures even if the weather repeats. This is an arrangement identity, not a claim of cross-platform full simulation replay validation. A retained previous manifest with the same signature blocks publication.
- `snapshotHash`: SHA-256 of the packed snapshot's UTF-8 bytes.
- `observationBundle`: the existing validated source bundle.
- `rulesId: daily-042-v1`, `simulationRevision: 0.42`, `fixedSettings: {handling: Balanced, condition: 0, startClock: 0}`, and `attemptLimits: {simulationSeconds: 90, elapsedSeconds: 600, submissionGraceSeconds: 120}`.
- `rankedServiceAvailable: false`: this publisher supplies observed wave identity; it cannot assert the existence or validation strength of a leaderboard service.

`manifestHash` is SHA-256 of the complete manifest encoded as compact, key-sorted ASCII JSON **with `manifestHash` omitted**, with no trailing newline. Hash verification must use that canonical encoding, not the pretty-printing or field order of a client serializer. The on-disk JSON adds one trailing newline. Both statuses have a manifest hash.

Validate observation freshness against `observationCutoffUtc`, then check the competition's current time window separately. Calling rolling-current `IsFresh` at play time would incorrectly expire the morning wave during its valid competition day. Use a server clock for ranked eligibility. A parsed or cached manifest alone does not grant score-submission eligibility.

## Integration boundary

The local `observations.yml` now runs both test commands, restores/fills `site/daily`, validates the manifest, and uploads it alongside rolling `current.json`. Its mechanical schedule adds minute 0 every hour, so 07:00 New York is covered in either daylight-saving season; the existing 7/27/47-minute retries remain. Scheduling can be delayed. No scheduled workflow has been enabled or deployed by this change.

The public repository source allowlist must add `daily-publisher.py`, `daily-checks.py` and `daily-README.md` alongside the existing importer, tests, README and workflow. Copy no Unity code, authoring history or raw source receipt directory. The Pages artifact retains a bounded history: today's and yesterday's dated files, plus `daily/current.json`. The leaderboard service must pin complete manifests for older challenges; the public feed is not a permanent historical archive. An existing durable output directory may retain more history locally. The workflow retains the prior rolling-feed failure policy: failed rolling-current validation stops the complete deployment, leaving the prior public artifact intact. It never substitutes stale observations into a daily freeze.

The 20 offline tests cover DST, cutoff selection, delayed retrieval, missing/stale/future/QC-failed observations, provenance, the packed contract, independent seeds, different daily arrangements, tampering, immutable publication, clean-checkout restoration, recovery from unavailable observations and day rollover. They do not prove provider availability, a deployed schedule, Unity behavior, physical-device behavior or a globally validated leaderboard.
