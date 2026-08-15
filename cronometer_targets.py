"""
Compute Cronometer's dynamic daily calorie target WITHOUT a browser.

Background
----------
Cronometer's daily "Energy target" (the denominator in "2227 / 3333 kcal")
is NOT sent down by the server as a field -- it is computed client-side in
the web app. So it does not appear in any export endpoint, and it cannot be
scraped from the API directly.

It can, however, be RECONSTRUCTED exactly, because it decomposes as:

    target = BMR + exercise_kcal + TEF

where
    BMR           a per-user constant (calorie burn at rest)
    exercise_kcal the sum of that day's exercise entries -- INCLUDING the
                  synced "Daily Activity (Garmin)" entry, which is what makes
                  the target swing day to day
    TEF           the thermic effect of food: the energy cost of digesting
                  what you ate, which is macronutrient-specific

Both `exercise_kcal` and the macros driving TEF ARE available from the
password-authenticated export API, so the whole target is computable in a
headless/cloud context.

Calibration (7 days: 2026-08-09 .. 2026-08-15)
--------------------------------------------------------
Fitting BMR and the three TEF coefficients simultaneously by least squares
against targets read off the Cronometer UI:

    BMR      = 1880.26 kcal/day
    protein  = 0.9489 kcal/g   (23.7% of protein calories)
    carb     = 0.2714 kcal/g   ( 6.8% of carb calories)
    fat      = 0.2824 kcal/g   ( 3.1% of fat calories)

    max residual 2.51 kcal, RMSE 1.20 kcal across all 7 days
    leave-one-out cross-validation worst error: 6.34 kcal (~0.2% of target)

Two independent confirmations that BMR = 1880 is real and not a fitted
artifact:
  * The two zero-intake days (Aug 10, Aug 11), where TEF must be exactly 0,
    give target - exercise = 1879.6 and 1880.3 -- agreeing to 0.7 kcal.
  * The 4-parameter free fit recovers 1880.26 on its own.

The fitted TEF percentages also land right on the physiological literature
values (protein ~20-30%, carbs ~5-10%, fat ~0-3%), which is what you'd
expect if this decomposition is the real mechanism rather than a curve fit.

CAVEATS
-------
* BMR is a calibration constant, not a law. It will drift as your weight /
  body composition / Cronometer profile settings change. Re-calibrate with
  `calibrate()` below whenever you have a fresh (target, date) observation
  -- ideally a zero-intake day, where BMR = target - exercise exactly.
* Coefficients were fit on 7 days with only 5 fed days. They cross-validate
  well, but more fed days would tighten them. `calibrate()` accepts more.
* Alcohol TEF is unidentified (every calibration day had 0 g alcohol). If
  you log alcohol, expect a small underestimate; literature TEF for alcohol
  is high (~10-30%).

Usage
-----
    export CRONOMETER_USERNAME=you@example.com
    export CRONOMETER_PASSWORD=yourpassword
    python cronometer_targets.py 2026-08-09 2026-08-15
"""

from __future__ import annotations

import csv
import io
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from cronometer_client import CronometerClient

# ---- Calibrated model constants -------------------------------------------
BMR_KCAL = 1880.26
TEF_PROTEIN_PER_G = 0.9489
TEF_CARB_PER_G = 0.2714
TEF_FAT_PER_G = 0.2824


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class DayTarget:
    day: date
    target: float          # reconstructed Cronometer energy target
    bmr: float
    exercise: float        # total kcal burned from exercise entries
    tef: float             # thermic effect of food
    consumed: float        # kcal actually eaten
    protein_g: float
    carb_g: float
    fat_g: float
    sleep_score: float | None = None   # Garmin sleep score, 0-100
    recovery: float | None = None      # Garmin recovery score, 0-100

    @property
    def remaining(self) -> float:
        return self.target - self.consumed

    @property
    def net(self) -> float:
        """Consumed minus exercise -- Cronometer's 'net' figure."""
        return self.consumed - self.exercise


def compute_tef(protein_g: float, carb_g: float, fat_g: float) -> float:
    return (
        TEF_PROTEIN_PER_G * protein_g
        + TEF_CARB_PER_G * carb_g
        + TEF_FAT_PER_G * fat_g
    )


def fetch_targets(
    client: CronometerClient,
    start: date,
    end: date,
    bmr: float = BMR_KCAL,
) -> list[DayTarget]:
    """Reconstruct the daily energy target for each day in the range."""
    nutrition = client.export_csv("daily", start, end)
    exercises = client.export_csv("exercises", start, end)
    biometrics = client.export_csv("biometrics", start, end)

    macros: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(nutrition)):
        macros[row["Date"]] = row

    burned: dict[str, float] = defaultdict(float)
    for row in csv.DictReader(io.StringIO(exercises)):
        # Cronometer reports exercise kcal as negative; we want magnitude.
        burned[row["Day"]] += abs(_f(row["Calories Burned"]))

    sleep_scores: dict[str, float] = {}
    recovery_scores: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(biometrics)):
        if row["Metric"] == "Sleep Score (Garmin)":
            sleep_scores[row["Day"]] = _f(row["Amount"])
        elif row["Metric"] == "Recovery (Garmin)":
            recovery_scores[row["Day"]] = _f(row["Amount"])

    out: list[DayTarget] = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        row = macros.get(key, {})
        protein = _f(row.get("Protein (g)"))
        carb = _f(row.get("Carbs (g)"))
        fat = _f(row.get("Fat (g)"))
        tef = compute_tef(protein, carb, fat)
        ex = burned.get(key, 0.0)
        out.append(
            DayTarget(
                day=cur,
                target=bmr + ex + tef,
                bmr=bmr,
                exercise=ex,
                tef=tef,
                consumed=_f(row.get("Energy (kcal)")),
                protein_g=protein,
                carb_g=carb,
                fat_g=fat,
                sleep_score=sleep_scores.get(key),
                recovery=recovery_scores.get(key),
            )
        )
        cur += timedelta(days=1)
    return out


def calibrate(
    client: CronometerClient,
    observed: dict[str, float],
    fit_tef: bool = False,
) -> dict:
    """
    Re-derive the model constants from observed targets read off the UI.

    `observed` maps "YYYY-MM-DD" -> target kcal shown in Cronometer.

    With fit_tef=False (default) only BMR is re-derived, holding the TEF
    coefficients fixed -- this is the right choice for routine re-calibration
    after a weight change, and works with as little as one observation.

    With fit_tef=True all four parameters are re-fit by least squares. Needs
    several fed days to be meaningful; prefer including at least one
    zero-intake day to pin BMR.
    """
    import numpy as np

    days = sorted(observed)
    start = datetime.strptime(days[0], "%Y-%m-%d").date()
    end = datetime.strptime(days[-1], "%Y-%m-%d").date()
    rows = {d.day.isoformat(): d for d in fetch_targets(client, start, end)}

    y = np.array([observed[d] - rows[d].exercise for d in days])

    if not fit_tef:
        tefs = np.array([rows[d].tef for d in days])
        bmr = float((y - tefs).mean())
        resid = y - tefs - bmr
        return {
            "bmr": bmr,
            "tef_protein_per_g": TEF_PROTEIN_PER_G,
            "tef_carb_per_g": TEF_CARB_PER_G,
            "tef_fat_per_g": TEF_FAT_PER_G,
            "max_abs_residual": float(np.abs(resid).max()),
            "n_days": len(days),
        }

    A = np.array([[1.0, rows[d].protein_g, rows[d].carb_g, rows[d].fat_g] for d in days])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    return {
        "bmr": float(coef[0]),
        "tef_protein_per_g": float(coef[1]),
        "tef_carb_per_g": float(coef[2]),
        "tef_fat_per_g": float(coef[3]),
        "max_abs_residual": float(np.abs(resid).max()),
        "n_days": len(days),
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <start YYYY-MM-DD> <end YYYY-MM-DD>")
        return 1

    username = os.environ.get("CRONOMETER_USERNAME")
    password = os.environ.get("CRONOMETER_PASSWORD")
    if not username or not password:
        print("set CRONOMETER_USERNAME and CRONOMETER_PASSWORD first")
        return 1

    start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    end = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()

    client = CronometerClient.login(username, password)
    try:
        rows = fetch_targets(client, start, end)
    finally:
        client.logout()

    hdr = (
        f"{'date':<12}{'target':>8}{'BMR':>8}{'exercise':>10}{'TEF':>8}{'eaten':>9}"
        f"{'left':>9}{'sleep':>7}{'recov':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        sleep = f"{r.sleep_score:.0f}" if r.sleep_score is not None else "-"
        recov = f"{r.recovery:.0f}" if r.recovery is not None else "-"
        print(
            f"{r.day.isoformat():<12}{r.target:8.0f}{r.bmr:8.0f}{r.exercise:10.0f}"
            f"{r.tef:8.0f}{r.consumed:9.0f}{r.remaining:9.0f}{sleep:>7}{recov:>7}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
