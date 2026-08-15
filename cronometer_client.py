"""
Unofficial Cronometer data connector.

Cronometer has no official public API. This module logs into cronometer.com
with your username/password (same as the web app) and calls the same
internal "export" and GWT-RPC endpoints the Single Page App uses to pull
CSV exports of your own data. It is READ-ONLY: there is no supported way
to write data (e.g. create recipes) through these endpoints.

Because this rides on Cronometer's internal, unversioned app API:
  - It can break whenever Cronometer ships a frontend update (the
    GWT_HEADER / GWT_PERMUTATION constants below are version-pinned
    strings that occasionally go stale -- see refresh instructions below).
  - It should only be used for your own personal data. Cronometer's forums
    note repeatedly that anything beyond personal use should go through
    an Enterprise/partner agreement with them directly.

Usage:
    export CRONOMETER_USERNAME=you@example.com
    export CRONOMETER_PASSWORD=yourpassword
    python cronometer_client.py servings 2026-08-01 2026-08-15
    python cronometer_client.py daily 2026-08-01 2026-08-15
    python cronometer_client.py exercises 2026-08-01 2026-08-15
    python cronometer_client.py biometrics 2026-08-01 2026-08-15
    python cronometer_client.py notes 2026-08-01 2026-08-15

If a request suddenly starts failing with an auth/token error, Cronometer
likely shipped a new frontend build. To refresh the constants:
  1. Open https://cronometer.com/login/ in Chrome, open DevTools > Network.
  2. Log in normally, and find a POST request to
     https://cronometer.com/cronometer/app with a text/x-gwt-rpc body.
  3. In the request headers, copy the values of `x-gwt-permutation` and
     the request body (the payload starts with "7|0|N|...|<HEADER>|...").
     The long hex string right after the module base URL is GWT_HEADER.
  4. Update GWT_HEADER / GWT_PERMUTATION below.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime

import requests

HTML_LOGIN_URL = "https://cronometer.com/login/"
API_LOGIN_URL = "https://cronometer.com/login"
GWT_BASE_URL = "https://cronometer.com/cronometer/app"
API_EXPORT_URL = "https://cronometer.com/export"

# These may need periodic refreshing -- see module docstring.
GWT_CONTENT_TYPE = "text/x-gwt-rpc; charset=UTF-8"
GWT_MODULE_BASE = "https://cronometer.com/cronometer/"
GWT_PERMUTATION = "7B121DC5483BF272B1BC1916DA9FA963"
GWT_HEADER = "2D6A926E3729946302DC68073CB0D550"

GWT_AUTHENTICATE = (
    f"7|0|5|{GWT_MODULE_BASE}|{GWT_HEADER}|com.cronometer.shared.rpc.CronometerService"
    "|authenticate|java.lang.Integer/3438268394|1|2|3|4|1|5|5|-300|"
)

GWT_GENERATE_AUTH_TOKEN_TMPL = (
    f"7|0|8|{GWT_MODULE_BASE}|{GWT_HEADER}|com.cronometer.shared.rpc.CronometerService"
    "|generateAuthorizationToken|java.lang.String/2004016611|I"
    "|com.cronometer.shared.user.AuthScope/2065601159|{nonce}|1|2|3|4|4|5|6|6|7|8|{userid}|3600|7|2|"
)

GWT_LOGOUT_TMPL = (
    f"7|0|6|{GWT_MODULE_BASE}|{GWT_HEADER}|com.cronometer.shared.rpc.CronometerService"
    "|logout|java.lang.String/2004016611|{nonce}|1|2|3|4|1|5|6|"
)

_TOKEN_RE = re.compile(r'"(.*)"')
_AUTH_RE = re.compile(r"OK\[(\d*),.*")

VALID_GENERATORS = {
    "servings": "servings",
    "daily": "dailySummary",
    "exercises": "exercises",
    "biometrics": "biometrics",
    "notes": "notes",
}


class CronometerError(RuntimeError):
    pass


@dataclass
class CronometerClient:
    session: requests.Session
    user_id: str = ""

    @classmethod
    def login(cls, username: str, password: str) -> "CronometerClient":
        session = requests.Session()

        # 1. Grab anti-CSRF token from the login page HTML.
        resp = session.get(HTML_LOGIN_URL)
        resp.raise_for_status()
        m = re.search(r'name="anticsrf"\s+value="([^"]*)"', resp.text)
        if not m:
            raise CronometerError("could not find anticsrf token on login page")
        anticsrf = m.group(1)

        # 2. Log in with username/password/anticsrf.
        resp = session.post(
            API_LOGIN_URL,
            data={"anticsrf": anticsrf, "username": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise CronometerError(f"login failed: {payload['error']}")

        client = cls(session=session)

        # 3. Authenticate against the GWT RPC endpoint to get a user id + sesnonce cookie.
        gwt_resp = client._gwt_request(GWT_AUTHENTICATE)
        m = _AUTH_RE.search(gwt_resp)
        if not m:
            raise CronometerError(f"could not parse GWT auth response: {gwt_resp[:200]!r}")
        client.user_id = m.group(1)
        return client

    def _gwt_request(self, body: str) -> str:
        headers = {
            "content-type": GWT_CONTENT_TYPE,
            "x-gwt-module-base": GWT_MODULE_BASE,
            "x-gwt-permutation": GWT_PERMUTATION,
        }
        resp = self.session.post(GWT_BASE_URL, data=body.encode("utf-8"), headers=headers)
        resp.raise_for_status()
        return resp.text

    def _sesnonce(self) -> str:
        nonce = self.session.cookies.get("sesnonce")
        if not nonce:
            raise CronometerError("missing sesnonce cookie; login may have failed")
        return nonce

    def _generate_auth_token(self) -> str:
        body = GWT_GENERATE_AUTH_TOKEN_TMPL.format(nonce=self._sesnonce(), userid=self.user_id)
        resp = self._gwt_request(body)
        m = _TOKEN_RE.search(resp)
        if not m:
            raise CronometerError(f"could not parse export token from: {resp[:200]!r}")
        return m.group(1)

    def export_csv(self, kind: str, start: date, end: date) -> str:
        if kind not in VALID_GENERATORS:
            raise ValueError(f"kind must be one of {sorted(VALID_GENERATORS)}")
        token = self._generate_auth_token()
        resp = self.session.get(
            API_EXPORT_URL,
            params={
                "nonce": token,
                "generate": VALID_GENERATORS[kind],
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        resp.raise_for_status()
        return resp.text

    def export_rows(self, kind: str, start: date, end: date) -> list[dict]:
        raw = self.export_csv(kind, start, end)
        return list(csv.DictReader(io.StringIO(raw)))

    def logout(self) -> None:
        self._gwt_request(GWT_LOGOUT_TMPL.format(nonce=self._sesnonce()))


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in VALID_GENERATORS:
        print(f"usage: {sys.argv[0]} <{'|'.join(VALID_GENERATORS)}> <start YYYY-MM-DD> <end YYYY-MM-DD>")
        return 1

    username = os.environ.get("CRONOMETER_USERNAME")
    password = os.environ.get("CRONOMETER_PASSWORD")
    if not username or not password:
        print("set CRONOMETER_USERNAME and CRONOMETER_PASSWORD environment variables first")
        return 1

    kind = sys.argv[1]
    start = _parse_date(sys.argv[2])
    end = _parse_date(sys.argv[3])

    client = CronometerClient.login(username, password)
    try:
        print(client.export_csv(kind, start, end))
    finally:
        client.logout()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
