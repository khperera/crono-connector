# cronometer-connector

Read-only Python client for [Cronometer](https://cronometer.com), plus an exact
reconstruction of Cronometer's dynamic daily calorie target.

Cronometer has **no official public API**. This talks to the same internal
endpoints the web app uses, authenticating with your own username and password.
Personal use only.

## What it does

**`cronometer_client.py`** — logs in and pulls CSV exports:

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
date          target     BMR  exercise     TEF    eaten     left
----------------------------------------------------------------
2026-08-09      3272    1880      1261     131     1254     2018
2026-08-10      2627    1880       746       0        0     2627
2026-08-11      2810    1880       930       0        0     2810
2026-08-12      3101    1880       995     225     2074     1027
2026-08-13      2911    1880       751     279     2564      347
2026-08-14      2842    1880       945      17      280     2562
2026-08-15      3333    1880      1251     201     2227     1106
```

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

- **This rides on an unversioned internal API.** `GWT_HEADER` and
  `GWT_PERMUTATION` in `cronometer_client.py` are version-pinned to a
  Cronometer frontend build and go stale when they ship an update, causing auth
  errors. Refresh instructions are in the module docstring.
- **Read-only.** There is no write access — recipes, food entries, and diary
  edits cannot be created through these endpoints.
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

Auth flow derived from [`gocronometer`](https://github.com/jrmycanady/gocronometer) (GPLv2), a Go client for the same endpoints.
