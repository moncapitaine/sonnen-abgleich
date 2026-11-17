"""Client for the sonnen customer portal (my.sonnen.de).

sonnen has no documented public API. This talks to the same endpoints the portal's
own web page uses, with the login the web page performs:

  1. OAuth authorization-code flow with PKCE against account.sonnen.de
     (a normal HTML login form with a CSRF token),
  2. bearer token against my-api.sonnen.de,
  3. the CSV endpoints behind the portal's "download" button.

If sonnen changes the portal this may stop working; the app then keeps running on
the data it has and CSV files can still be imported by hand.
"""
import base64
import hashlib
import os
import re
import secrets
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

AUTH_URL = "https://account.sonnen.de"
API_V1 = "https://my-api.sonnen.de/v1"
API_V2 = "https://my-api.sonnen.de/v2"
# Public client id of the portal web app (desktop variant), taken from my.sonnen.de.
CLIENT_ID = os.environ.get("SONNEN_CLIENT_ID", "a80a9abfd8c2847370340c96a04d2af7c691cce4c7cac58d8e12012f5da9280c")
REDIRECT_URI = "https://my.sonnen.de/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
STAT_FIELDS = ("produced_energy,consumed_energy,battery_charged_energy,"
               "battery_discharged_energy,grid_feedin_energy,grid_purchase_energy")


class PortalError(RuntimeError):
    pass


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SonnenPortal:
    def __init__(self, username: str, password: str, refresh_token: str | None = None):
        self.username, self.password = username, password
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.http = httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=False)

    def close(self):
        self.http.close()

    # ---- authentication ------------------------------------------------------

    def authenticate(self) -> None:
        if self.refresh_token:
            try:
                self._token({"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                             "client_id": CLIENT_ID})
                return
            except PortalError:
                self.refresh_token = None  # expired or revoked: fall back to a full login
        self._login()

    def _token(self, form: dict) -> None:
        r = self.http.post(f"{AUTH_URL}/oauth/token", data=form, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise PortalError(f"Token-Abruf fehlgeschlagen (HTTP {r.status_code}).")
        body = r.json()
        self.access_token = body.get("access_token")
        self.refresh_token = body.get("refresh_token") or self.refresh_token
        if not self.access_token:
            raise PortalError("Token-Antwort ohne access_token.")

    def _login(self) -> None:
        if not self.username or not self.password:
            raise PortalError("SONNEN_USERNAME / SONNEN_PASSWORD sind nicht gesetzt.")
        verifier = _b64(secrets.token_bytes(64))
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        r = self._follow(self.http.get(f"{AUTH_URL}/oauth/authorize", params={
            "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "nonce": secrets.token_hex(8), "locale": "de"}))
        code = self._code(r)
        if not code:
            form = re.search(r"<form[^>]*action=\"([^\"]*sign_in[^\"]*)\"[^>]*>(.*?)</form>", r.text, re.S)
            token = re.search(r"name=\"authenticity_token\"[^>]*value=\"([^\"]+)\"", r.text)
            if not form or not token:
                raise PortalError("Login-Formular des sonnen-Portals nicht erkannt (Portal geändert?).")
            r = self._follow(self.http.post(urljoin(str(r.url), unescape(form.group(1))), data={
                "authenticity_token": unescape(token.group(1)),
                "user[email]": self.username, "user[password]": self.password,
                "user[remember_me]": "0", "commit": "Log in"}))
            code = self._code(r)
        if not code:
            if "sign_in" in str(r.url):
                raise PortalError("Anmeldung abgelehnt - Benutzername/Passwort prüfen "
                                  "(oder das Portal verlangt eine zusätzliche Bestätigung).")
            raise PortalError(f"Kein Autorisierungscode erhalten (gelandet auf {urlsplit(str(r.url)).path}).")
        self._token({"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
                     "redirect_uri": REDIRECT_URI, "code_verifier": verifier})

    def _follow(self, r: httpx.Response) -> httpx.Response:
        """Follow redirects by hand and stop at the redirect back to the portal (it carries the code)."""
        for _ in range(12):
            if r.status_code not in (301, 302, 303, 307, 308):
                return r
            target = urljoin(str(r.url), r.headers.get("location", ""))
            if target.startswith(REDIRECT_URI):
                r.extensions["final_location"] = target
                return r
            r = self.http.get(target)
        raise PortalError("Zu viele Weiterleitungen beim Login.")

    @staticmethod
    def _code(r: httpx.Response) -> str | None:
        target = r.extensions.get("final_location")
        if not target:
            return None
        return (parse_qs(urlsplit(target).query).get("code") or [None])[0]

    # ---- data ----------------------------------------------------------------

    def _get(self, url: str, params=None, accept="application/vnd.api+json") -> httpx.Response:
        if not self.access_token:
            self.authenticate()
        for attempt in (1, 2):
            r = self.http.get(url, params=params, headers={
                "Authorization": f"Bearer {self.access_token}", "Accept": accept})
            if r.status_code == 401 and attempt == 1:
                self.access_token = None
                self.authenticate()
                continue
            break
        if r.status_code != 200:
            raise PortalError(f"Portal-Abfrage {urlsplit(url).path} fehlgeschlagen (HTTP {r.status_code}).")
        return r

    def sites(self) -> list[dict]:
        """The installations (houses) of this account: [{'id': ..., 'name': ...}]."""
        me = self._get(f"{API_V1}/users/me").json()
        user_id = (me.get("data") or {}).get("id")
        if not user_id:
            raise PortalError("Benutzer-ID im Portal nicht gefunden.")
        body = self._get(f"{API_V1}/users/{user_id}/sites").json()
        out = []
        for item in body.get("data") or []:
            attrs = item.get("attributes") or {}
            name = " ".join(str(attrs.get(k) or "") for k in ("street", "house_number", "city")).strip()
            out.append({"id": item.get("id"), "name": name or attrs.get("name") or ""})
        return out

    def _csv(self, site_id: str, kind: str, start: datetime, end: datetime, resolution: str) -> str:
        params = {"filter[start_at]": _iso(start), "filter[end_at]": _iso(end),
                  "filter[resolution]": resolution}
        if kind == "statistics":
            params["filter[fields]"] = STAT_FIELDS
        r = self._get(f"{API_V2}/sites/{site_id}/{kind}.csv", params=params, accept="text/csv, */*")
        return r.text

    def statistics_csv(self, site_id, start, end, resolution="1-day") -> str:
        return self._csv(site_id, "statistics", start, end, resolution)

    def measurements_csv(self, site_id, start, end, resolution="3-hours") -> str:
        return self._csv(site_id, "measurements", start, end, resolution)
