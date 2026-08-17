"""
Cronometer diary connector: recent items, food search, and diary writes.

This talks to Cronometer's MOBILE app API (mobile.cronometer.com/api/v2/*),
which is a separate, cleaner JSON API from the GWT-RPC endpoints used by
cronometer_client.py for CSV exports. Unlike the GWT endpoints, the mobile
API supports writes -- this is what makes adding diary entries possible.

Endpoints were identified by authenticating against the live API with a real
account and probing candidate request bodies, mirroring the same "log in,
call an endpoint, inspect the response" approach used to reverse-engineer
cronometer_client.py's GWT endpoints. Confirmed against a live account:
  - POST /api/v2/login            -- auth, returns userId/sessionKey/timezone
  - POST /api/v2/find_food        -- search the food database or "tab": "CUSTOM"
                                      (the user's own foods/recipes)
  - POST /api/v2/get_recent_foods -- recently-logged foods (includes recipes)
  - POST /api/v2/get_food         -- full detail (measures, nutrients) for one food
  - POST /api/v2/get_foods        -- batched get_food, one request for many ids
  - POST /api/v2/add_serving      -- add a diary entry at a specific amount
  - POST /api/v2/get_diary        -- read a day's diary entries

Recipes have no separate endpoint: a Cronometer "recipe" is just a Custom
food whose default measure is named "full recipe" (alongside per-gram and
"Serving" measures). `list_recipes()` filters for that.

For a food whose measure is a weight (grams, oz, cup, ...), `amount` in
add_serving() is grams. For a food whose measure is "full recipe" or
"Serving" (i.e. a recipe), `amount` is a serving COUNT (1.0 = one full
recipe), not grams -- this mirrors how the app itself sends these entries.

Rate limiting
-------------
Cronometer throttles repeated logins on this endpoint hard enough to lock an
account out for several minutes ("Too Many Attempts") -- this was hit during
development, from little more than a dozen logins in quick succession while
testing. Two things here are aimed straight at that:

  - Every request (login included) goes through a shared rate limiter that
    enforces a minimum gap between calls and retries with backoff on a
    rate-limit response instead of failing immediately.
  - The session (user id + token) is cached to disk after login and reused
    by later invocations, so running the CLI repeatedly -- e.g. one command
    per food while logging a meal -- costs one login total, not one per
    call. get_foods()/add_servings() also let one *client-side* call cover
    several foods/servings, each still a separate HTTP request but sharing
    the one cached session and the shared rate limiter's spacing, which is
    what actually keeps a batch from tripping the limit. There is no single
    HTTP call that logs multiple servings at once -- no such endpoint was
    found.

Usage:
    export CRONOMETER_USERNAME=you@example.com
    export CRONOMETER_PASSWORD=yourpassword
    python cronometer_diary.py search "banana"
    python cronometer_diary.py recent
    python cronometer_diary.py recipes
    python cronometer_diary.py food 450856 [462346 ...]
    python cronometer_diary.py add 450856 998940 118
    python cronometer_diary.py add-batch '[{"food_id":450856,"measure_id":998940,"amount":118}, ...]'
    python cronometer_diary.py diary 2026-08-17

Caveats:
  - This rides on Cronometer's internal, unversioned mobile app API. It can
    break whenever Cronometer ships a new app build, the same risk called
    out for the GWT endpoints in cronometer_client.py.
  - Personal use only, same as the rest of this project.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

MOBILE_BASE_URL = "https://mobile.cronometer.com"

# Sent with every request to look like the Android app.
_APP_AUTH_TEMPLATE = {"api": 3, "os": "Android", "build": "2807", "flavour": "free"}

_SESSION_CACHE_PATH = (
    Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "cronometer-connector"
    / "session.json"
)


class CronometerError(RuntimeError):
    pass


class _RateLimiter:
    """Enforces a minimum gap between requests and retries with exponential
    backoff when Cronometer itself rate-limits us -- either HTTP 429, or (as
    observed live) an HTTP-200 body of {"result": "FAIL", "error": "Too Many
    Attempts. Please try again later."}."""

    def __init__(
        self,
        min_interval: float = 1.5,
        max_retries: int = 5,
        base_backoff: float = 20.0,
        max_backoff: float = 180.0,
    ):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._last_call = 0.0

    def send(self, post_fn) -> requests.Response:
        """Call post_fn() -> requests.Response, pacing and retrying as needed."""
        attempt = 0
        while True:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()

            resp = post_fn()
            if not self._is_rate_limited(resp):
                return resp
            if attempt >= self.max_retries:
                raise CronometerError(
                    "Cronometer is still rate-limiting this account after "
                    f"{self.max_retries} retries; wait a few minutes before trying again."
                )
            delay = min(self.base_backoff * (2**attempt), self.max_backoff) + random.uniform(0, 1)
            time.sleep(delay)
            attempt += 1

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        return (
            isinstance(data, dict)
            and data.get("result") == "FAIL"
            and "too many attempts" in str(data.get("error", "")).lower()
        )


# Shared across all clients in this process, since the limit is enforced by
# Cronometer per-account/IP, not per client instance.
_rate_limiter = _RateLimiter()


def _load_cached_session(username: str) -> dict | None:
    try:
        data = json.loads(_SESSION_CACHE_PATH.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if data.get("username") != username:
        return None
    return data


def _save_cached_session(username: str, user_id: int, token: str, timezone: str) -> None:
    try:
        _SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SESSION_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"username": username, "user_id": user_id, "token": token, "timezone": timezone})
        )
        os.replace(tmp, _SESSION_CACHE_PATH)
        os.chmod(_SESSION_CACHE_PATH, 0o600)
    except OSError:
        pass  # cache is a pure optimization; failing to write it is not fatal


def _clear_cached_session() -> None:
    try:
        _SESSION_CACHE_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _new_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "user-agent": "Dart/3.9 (dart:io)",
            "content-type": "text/plain; charset=utf-8",
            "accept-encoding": "gzip",
        }
    )
    return session


@dataclass
class CronometerDiaryClient:
    session: requests.Session
    user_id: int
    token: str
    timezone: str = "UTC"
    username: str = ""
    password: str = field(default="", repr=False)

    @classmethod
    def login(cls, username: str, password: str, *, use_cache: bool = True) -> "CronometerDiaryClient":
        """Authenticate. Reuses a cached session for this username when one
        exists, skipping the network call entirely -- pass use_cache=False to
        force a fresh login."""
        if use_cache:
            cached = _load_cached_session(username)
            if cached:
                return cls(
                    session=_new_http_session(),
                    user_id=cached["user_id"],
                    token=cached["token"],
                    timezone=cached.get("timezone", "UTC"),
                    username=username,
                    password=password,
                )
        return cls._fresh_login(username, password)

    @classmethod
    def _fresh_login(cls, username: str, password: str) -> "CronometerDiaryClient":
        session = _new_http_session()
        payload = {
            "email": username,
            "password": password,
            # Must stay null -- a non-null value here overwrites the account's
            # server-side timezone setting on login.
            "timezone": None,
            "userCode": None,
            "build": "4.48.2 b2807-a",
            "device": "Android 14 (SDK 34), Google Pixel 6 Pro",
            "firebaseToken": "",
            "features": {
                "food_search_config": '{"newSearch": true, "newSpellcheck": true}',
                "use_gpt_autofill": "true",
            },
            "auth": {"userId": None, "token": None, **_APP_AUTH_TEMPLATE},
            "lastSeen": 0,
            "config": {"call_version": 2},
        }
        resp = _rate_limiter.send(
            lambda: session.post(f"{MOBILE_BASE_URL}/api/v2/login", json=payload)
        )
        resp.raise_for_status()
        data = resp.json()
        if "sessionKey" not in data:
            raise CronometerError(f"login failed: {data}")

        timezone = data.get("timezone") or "UTC"
        _save_cached_session(username, data["id"], data["sessionKey"], timezone)
        return cls(
            session=session,
            user_id=data["id"],
            token=data["sessionKey"],
            timezone=timezone,
            username=username,
            password=password,
        )

    def _auth_block(self) -> dict:
        return {"userId": self.user_id, "token": self.token, **_APP_AUTH_TEMPLATE}

    def _call(self, endpoint: str, payload: dict, *, _retried: bool = False) -> dict:
        body = dict(payload)
        body["auth"] = self._auth_block()
        body.setdefault("lastSeen", 0)
        resp = _rate_limiter.send(
            lambda: self.session.post(f"{MOBILE_BASE_URL}{endpoint}", json=body)
        )
        resp.raise_for_status()
        data = resp.json()

        auth_failed = isinstance(data, dict) and data.get("result") == "FAIL" and (
            "session" in str(data.get("error", "")).lower()
            or "auth" in str(data.get("error", "")).lower()
        )
        if auth_failed and not _retried and self.password:
            # Cached session was stale -- drop it, log in for real, retry once.
            _clear_cached_session()
            fresh = self._fresh_login(self.username, self.password)
            self.session, self.user_id, self.token, self.timezone = (
                fresh.session,
                fresh.user_id,
                fresh.token,
                fresh.timezone,
            )
            return self._call(endpoint, payload, _retried=True)

        if isinstance(data, dict) and data.get("result") == "FAIL":
            raise CronometerError(f"{endpoint} failed: {data.get('error')}")
        return data

    def _format_day(self, day: date | None = None) -> str:
        d = day or datetime.now().date()
        return f"{d.year}-{d.month}-{d.day}"

    # -- Search / read --------------------------------------------------

    def search_food(self, query: str, tab: str = "ALL") -> list[dict]:
        """Search the food database (tab="ALL") or the user's own foods/recipes
        (tab="CUSTOM"). Each result includes an `id`, `measureId` (its default
        serving), and `measureDisplayName`."""
        payload = {
            "query": query,
            "tab": tab,
            "sources": ["All"],
            "config": {"newSearch": True, "newSpellcheck": True, "call_version": 1},
        }
        return self._call("/api/v2/find_food", payload).get("foods", [])

    def get_recent_foods(self) -> list[dict]:
        """Recently-logged foods, each as {"count": int, "food": {...}}.
        Includes user recipes (source "Custom" with a "full recipe" measure)
        alongside database foods."""
        return self._call(
            "/api/v2/get_recent_foods", {"config": {"call_version": 1}}
        ).get("servings", [])

    def list_recipes(self) -> list[dict]:
        """The user's saved recipes: custom foods whose default measure is
        "full recipe" (as opposed to a plain custom food logged by weight)."""
        foods = self.search_food("", tab="CUSTOM")
        return [f for f in foods if "full recipe" in (f.get("measureDisplayName") or "")]

    def get_food(self, food_id: int) -> dict:
        """Full detail for one food/recipe: available measures and nutrients."""
        return self._call(
            "/api/v2/get_food", {"id": food_id, "config": {"call_version": 1}}
        )

    def get_foods(self, food_ids: list[int]) -> list[dict]:
        """Full detail for many foods/recipes in a single request -- use this
        instead of calling get_food() in a loop when you need more than one."""
        if not food_ids:
            return []
        data = self._call(
            "/api/v2/get_foods", {"ids": list(food_ids), "config": {"call_version": 1}}
        )
        return data.get("foods", [])

    def get_diary(self, day: date | None = None) -> list[dict]:
        """All diary entries for a given day (defaults to today)."""
        return self._call(
            "/api/v2/get_diary",
            {"day": self._format_day(day), "config": {"call_version": 1}},
        ).get("diary", [])

    # -- Write ------------------------------------------------------------

    def add_serving(
        self,
        food_id: int,
        measure_id: int,
        amount: float,
        translation_id: int = 0,
        day: date | None = None,
        diary_group: int = 0,
    ) -> dict:
        """Add a diary entry for `food_id` at `amount` of `measure_id`.

        `amount` is grams for weight-style measures, or a serving count for
        "full recipe" / "Serving" style measures -- match whichever unit
        `measure_id` refers to (see get_food()/search_food() results).

        `diary_group`: 0 = auto-assign from current time of day,
        1 = Breakfast, 2 = Lunch, 3 = Dinner, 4 = Snacks.
        """
        now = datetime.now()
        if diary_group == 0:
            diary_group = _meal_group_for_hour(now.hour)
        serving = {
            "order": (diary_group << 16) | 1,
            "day": self._format_day(day),
            "time": f"{now.hour}:{now.minute}:{now.second}",
            "offset": None,
            "source": None,
            "userId": self.user_id,
            "servingId": None,
            "type": "Serving",
            "foodId": food_id,
            "measureId": measure_id,
            "grams": amount,
            "translationId": translation_id,
        }
        return self._call(
            "/api/v2/add_serving", {"serving": serving, "config": {"call_version": 2}}
        )

    def add_servings(self, items: list[dict]) -> list[dict]:
        """Add several diary entries in one client-side call.

        Each item is a dict of add_serving() kwargs, e.g.
        {"food_id": 450856, "measure_id": 998940, "amount": 118}. Still one
        HTTP request per item (Cronometer has no multi-serving write
        endpoint), but they share this client's cached session and go
        through the same rate limiter, so a whole meal logged this way
        costs at most one login instead of one per item.
        """
        return [self.add_serving(**item) for item in items]


def _meal_group_for_hour(hour: int) -> int:
    if 4 <= hour < 10:
        return 1  # Breakfast
    elif 10 <= hour < 14:
        return 2  # Lunch
    elif 14 <= hour < 21:
        return 3  # Dinner
    else:
        return 4  # Snacks


def _login_from_env() -> CronometerDiaryClient:
    username = os.environ.get("CRONOMETER_USERNAME")
    password = os.environ.get("CRONOMETER_PASSWORD")
    if not username or not password:
        print("set CRONOMETER_USERNAME and CRONOMETER_PASSWORD environment variables first")
        raise SystemExit(1)
    return CronometerDiaryClient.login(username, password)


def _print_food_row(f: dict) -> None:
    print(f"{f.get('id'):<12} {f.get('measureId'):<12} {f.get('name','')[:40]:<40} {f.get('measureDisplayName','')}")


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage:\n"
            f"  {sys.argv[0]} search <query>\n"
            f"  {sys.argv[0]} recent\n"
            f"  {sys.argv[0]} recipes\n"
            f"  {sys.argv[0]} food <food_id> [food_id ...]\n"
            f"  {sys.argv[0]} add <food_id> <measure_id> <amount> [YYYY-MM-DD]\n"
            f"  {sys.argv[0]} add-batch <json array of {{food_id,measure_id,amount,day?}}>\n"
            f"  {sys.argv[0]} diary [YYYY-MM-DD]"
        )
        return 1

    cmd = sys.argv[1]
    client = _login_from_env()

    if cmd == "search":
        if len(sys.argv) != 3:
            print(f"usage: {sys.argv[0]} search <query>")
            return 1
        for f in client.search_food(sys.argv[2]):
            _print_food_row(f)

    elif cmd == "recent":
        for entry in client.get_recent_foods():
            f = entry.get("food", {})
            print(f"{f.get('id'):<12} count={entry.get('count'):<4} {f.get('name','')[:40]:<40} {f.get('source','')}")

    elif cmd == "recipes":
        for f in client.list_recipes():
            _print_food_row(f)

    elif cmd == "food":
        if len(sys.argv) < 3:
            print(f"usage: {sys.argv[0]} food <food_id> [food_id ...]")
            return 1
        foods = client.get_foods([int(a) for a in sys.argv[2:]])
        for food in foods:
            print(f"{food.get('name')} (id={food.get('id')})")
            for m in food.get("measures", []):
                print(f"  measure {m.get('id'):<12} {m.get('name')}")

    elif cmd == "add":
        if len(sys.argv) not in (5, 6):
            print(f"usage: {sys.argv[0]} add <food_id> <measure_id> <amount> [YYYY-MM-DD]")
            return 1
        day = datetime.strptime(sys.argv[5], "%Y-%m-%d").date() if len(sys.argv) == 6 else None
        result = client.add_serving(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), day=day)
        print(json.dumps(result, indent=2))

    elif cmd == "add-batch":
        if len(sys.argv) != 3:
            print(f"usage: {sys.argv[0]} add-batch <json array of {{food_id,measure_id,amount,day?}}>")
            return 1
        items = json.loads(sys.argv[2])
        for item in items:
            if "day" in item and item["day"]:
                item["day"] = datetime.strptime(item["day"], "%Y-%m-%d").date()
        results = client.add_servings(items)
        print(json.dumps(results, indent=2))

    elif cmd == "diary":
        day = datetime.strptime(sys.argv[2], "%Y-%m-%d").date() if len(sys.argv) == 3 else None
        for e in client.get_diary(day):
            print(e)

    else:
        print(f"unknown command: {cmd}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
