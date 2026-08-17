# cronometer-connector

Python client for [Cronometer](https://cronometer.com): CSV exports, an exact
reconstruction of Cronometer's dynamic daily calorie target, and diary
read/write (recent items, food search, adding entries at specific amounts).

Cronometer has **no official public API**. This talks to the same internal
endpoints the apps use, authenticating with your own username and password.
Personal use only.

## What it does

**`cronometer_diary.py`** — recent items, food/recipe search, and diary writes,
via Cronometer's mobile app API (`mobile.cronometer.com`):

```bash
export CRONOMETER_USERNAME=you@example.com
export CRONOMETER_PASSWORD=yourpassword

python cronometer_diary.py search "banana"     # search the food database
python cronometer_diary.py recent              # recently-logged items
python cronometer_diary.py recipes             # your saved recipes
python cronometer_diary.py food 450856         # measures/units for one food
python cronometer_diary.py add 450856 998940 118   # log 118 (of that measure) to today's diary
python cronometer_diary.py diary               # today's diary entries
```

`add` takes a `food_id` and `measure_id` from `search`/`recent`/`recipes`/`food`
output, and an `amount` in that measure's unit — grams for weight-style
measures, or a serving count (1.0 = one full recipe) for recipe/serving-style
measures. See the module docstring in `cronometer_diary.py` for the endpoint
details (`find_food`, `get_recent_foods`, `add_serving`, ...), reverse-engineered
by authenticating against a live account and inspecting request/response pairs.

Two things keep repeated use from tripping Cronometer's login rate limit
(hit during development after about a dozen logins in quick succession):
the session token is cached to disk and reused across invocations (one
login covers many commands, not one per command), and every request goes
through a shared limiter that paces calls and backs off automatically on a
rate-limit response. `food` accepts multiple ids and `add-batch` accepts a
JSON array of entries — both make several diary/food lookups under one
cached session instead of one login each, though each is still its own
HTTP request; Cronometer has no endpoint that logs multiple servings in a
single call.

**`cronometer_client.py`** — logs in and pulls CSV exports (read-only, via a
different internal API — Cronometer's GWT-RPC endpoints):

| kind | contents |
|---|---|
| `servings` | individual food entries |
| `daily` | daily nutrition summary (all micros + macros) |
| `exercises` | exercise entries, including synced wearable activity |
| `biometrics` | weight, body fat, HR, sleep, etc. |
| `notes` | diary notes |

```bash
export CRONOMETER_USERNAME=you@example.com
export CRONOMETER_PASSWORD=yourpassword

python cronometer_client.py daily 2026-08-01 2026-08-15
```

**`cronometer_targets.py`** — reconstructs the daily **energy target** (the
denominator in `2227 / 3333 kcal`).

```bash
python cronometer_targets.py 2026-08-09 2026-08-15
```

```
date          target     BMR  exercise     TEF    eaten     left  sleep  recov
------------------------------------------------------------------------------
2026-08-09      3272    1880      1261     131     1254     2018     70     76
2026-08-10      2627    1880       746       0        0     2627     78     87
2026-08-11      2810    1880       930       0        0     2810     72     86
2026-08-12      3101    1880       995     225     2074     1027     62     89
2026-08-13      2911    1880       751     279     2564      347     78     84
2026-08-14      2842    1880       945      17      280     2562     80     85
2026-08-15      3382    1880      1251     250     2741      641     76     88
```

`sleep` and `recov` are Garmin's Sleep Score and Recovery Score (0-100), pulled
from the `biometrics` export. They show as `-` for days with no synced value.

## Why the target needed reconstructing

The energy target is **not** a field any endpoint returns. Network capture of
the internal `getDayInfo` and `getCalendarInfo` RPC calls confirms the server
sends only raw logged entries — the app computes the target client-side. So it
can't be fetched, and scraping it needs a live browser session.

But it decomposes exactly:

```
target = BMR + exercise_kcal + TEF
```

- **BMR** — a per-user constant
- **exercise_kcal** — the day's exercise entries, *including* the synced
  "Daily Activity" entry from a wearable. This is what makes the target swing
  day to day (2626–3333 kcal over one week in testing).
- **TEF** — thermic effect of food, the energy cost of digestion, which is
  macronutrient-specific

Both `exercise_kcal` and the macros driving TEF *are* exportable, so the whole
target is computable headlessly.

### Calibration

Fitting BMR and the three TEF coefficients simultaneously by least squares
against targets read off the UI (7 days):

| parameter | value | as % of that macro's calories | literature |
|---|---|---|---|
| BMR | 1880.26 kcal/day | — | — |
| protein | 0.9489 kcal/g | 23.7% | 20–30% |
| carb | 0.2714 kcal/g | 6.8% | 5–10% |
| fat | 0.2824 kcal/g | 3.1% | 0–3% |

Max residual **2.51 kcal**, RMSE 1.20 kcal across all 7 days.
Leave-one-out cross-validation worst error: 6.34 kcal (~0.2% of target).

Two independent confirmations that BMR = 1880 is real and not a fitted artifact:

- The two **zero-intake days**, where TEF must be exactly 0, give
  `target − exercise` = 1879.6 and 1880.3 — agreeing to 0.7 kcal.
- The 4-parameter free fit recovers 1880.26 on its own.

That all three TEF coefficients land inside the accepted physiological ranges
is the strongest signal this is the real mechanism rather than a curve fit that
happens to work.

### Re-calibrating

BMR drifts with weight and body composition. `calibrate()` re-derives it:

```python
from cronometer_client import CronometerClient
from cronometer_targets import calibrate

client = CronometerClient.login(user, pw)
print(calibrate(client, {"2026-08-10": 2626}))   # one zero-intake day is enough
```

Pass `fit_tef=True` with several fed days to re-fit all four parameters.

## Caveats

- **This rides on unversioned internal APIs.** `GWT_HEADER` and
  `GWT_PERMUTATION` in `cronometer_client.py` are version-pinned to a
  Cronometer web build and go stale when they ship an update, causing auth
  errors — refresh instructions are in that module's docstring.
  `cronometer_diary.py` rides on the mobile app's API instead, pinned to an
  Android build string (`build`/`device` in `CronometerDiaryClient.login`);
  it can go stale the same way if Cronometer changes what that endpoint
  accepts.
- **`cronometer_client.py` is read-only** — CSV exports only, no write access.
  **`cronometer_diary.py` can write** — it adds real entries to your diary via
  `add_serving`/`add`. There's no undo command built in; delete a bad entry
  from the Cronometer app itself if needed.
- **Login rate limiting.** Cronometer throttles repeated logins on the mobile
  endpoint hard enough to lock an account out for several minutes ("Too Many
  Attempts"). `cronometer_diary.py` mitigates this with a disk-cached session
  (login once, reuse across CLI invocations) and a shared rate limiter with
  backoff on all requests, but the lockout is enforced server-side — if you
  do trip it (e.g. a burst of fresh-login testing with `use_cache=False`),
  the only fix is to wait it out.
- **Alcohol TEF is unidentified** — every calibration day had 0 g alcohol.
  Expect a small underestimate on drinking days; literature alcohol TEF is high
  (~10–30%).
- **Personal use only.** Cronometer directs anything beyond that to an
  Enterprise/partner agreement.

## Install

```bash
pip install -r requirements.txt
```

## Prior art

GWT export auth flow derived from [`gocronometer`](https://github.com/jrmycanady/gocronometer)
(GPLv2), a Go client for the same endpoints. Mobile API endpoint shapes for
`cronometer_diary.py` cross-checked against
[`cronometer-api-mcp`](https://github.com/rwestergren/cronometer-api-mcp), an
MCP server reverse-engineered against the same `mobile.cronometer.com` API,
then independently confirmed by authenticating against a live account.
