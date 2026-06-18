#!/usr/bin/env python3
"""
MyCupra ReadOnly - Login & Download Client für das EU Data Act Portal
(eu-data-act.drivesomethinggreater.com)

Verwendet ausschließlich die Python-Standardbibliothek (urllib, http.cookiejar) -
keine externen Pakete nötig, daher in manifest.json requirements: [].

Dieses Modul ist UI-/Framework-unabhängig (kein Bezug zu Home Assistant selbst)
und kann sowohl von der HA-Integration (coordinator.py) als auch eigenständig
importiert/getestet werden.
"""

import base64
import http.cookiejar
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feste Konstanten des OAuth-Flows (über manuelle Tests am 17.06.2026 verifiziert)
# ---------------------------------------------------------------------------
CLIENT_ID = "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com"
SCOPE = "openid cars profile"
STATE = "de__en__CUPRA"
REDIRECT_URI = "https://eu-data-act.drivesomethinggreater.com/login"
IDENTITY_BASE = "https://identity.vwgroup.io"
PORTAL_BASE = "https://eu-data-act.drivesomethinggreater.com"

# Default-Wert für die Anfrage-ID im Config-Flow. Bewusst LEER gelassen:
# jeder Nutzer hat seine eigene, im EU-Data-Act-Portal selbst angelegte
# Daueranfrage mit eigener Identifier-ID. Ein vorausgefüllter Wert (z.B. die
# ID eines bestimmten Nutzers) wäre für alle anderen Nutzer falsch und sollte
# daher nicht als Default in der Integration stehen.
# Der Identifier wird automatisch nach dem Login via fetch_request_identifier()
# aus dem Portal ausgelesen (GET /metadata/partial), falls er leer bleibt.
DEFAULT_REQUEST_IDENTIFIER = ""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


class CupraLoginError(Exception):
    """Basis-Fehlerklasse. Wird bei fehlgeschlagenem Login oder Datenabruf ausgelöst."""


class CupraRetryableError(CupraLoginError):
    """Fehler, bei denen ein erneuter Versuch sinnvoll sein kann:
    Netzwerkprobleme (kurzer Internetausfall) oder ein vom Server abgelehnter/
    abgelaufener Token (HTTP 401), der durch einen frischen Login behoben wird."""


class CupraPermanentError(CupraLoginError):
    """Fehler, bei denen ein erneuter Versuch garantiert wieder fehlschlägt:
    falsches Passwort, falsche VIN, ungültiger Anfrage-Identifier. Hier soll
    sofort und klar abgebrochen werden, statt wiederholt zu versuchen (würde
    nur Zeit verschwenden und könnte wie ein Brute-Force-Versuch wirken)."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Verhindert automatisches Folgen von Redirects, damit wir jeden Schritt
    selbst steuern können (genau wie curl ohne -L)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CupraClient:
    TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60
    REQUEST_TIMEOUT_SECONDS = 15
    RETRY_SCHEDULE = [
        (10, 10),
        (10, 60),
        (10, 600),
        (10, 1200),
    ]
    RETRY_FINAL_DELAY_SECONDS = 3600

    def __init__(self, email: str, password: str, vin: str,
                 request_identifier: str = DEFAULT_REQUEST_IDENTIFIER,
                 retry_speedup: float = 1.0):
        self.email = email
        self.password = password
        self.vin = vin
        self.request_identifier = request_identifier
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            NoRedirectHandler(),
        )
        self._token_expires_at = None
        self._retry_speedup = retry_speedup

    def _request(self, method, url, data=None, headers=None, allow_404=False):
        req_headers = {"User-Agent": USER_AGENT}
        if headers:
            req_headers.update(headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            resp = self.opener.open(req, timeout=self.REQUEST_TIMEOUT_SECONDS)
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            content = e.read()
            if status in (301, 302, 303, 307, 308) or allow_404:
                return status, e.headers, content
            error_text = content[:300].decode('utf-8', errors='replace')
            if status == 401:
                raise CupraRetryableError(f"HTTP 401 bei {url} (Token ungültig): {error_text}")
            raise CupraLoginError(f"HTTP {status} bei {url}: {error_text}")
        except urllib.error.URLError as e:
            raise CupraRetryableError(f"Netzwerkfehler bei {url}: {e.reason}")

    @staticmethod
    def _extract_hidden_inputs(html: str) -> dict:
        result = {}
        for name in ("_csrf", "relayState", "hmac"):
            m = re.search(rf'name="{name}"\s+value="([^"]*)"', html)
            if m:
                result[name] = m.group(1)
        return result

    @staticmethod
    def _extract_js_model_fields(html: str) -> dict:
        result = {}
        m = re.search(r"csrf_token:\s*'([^']*)'", html)
        if m:
            result["_csrf"] = m.group(1)
        m = re.search(r'"hmac":"([^"]*)"', html)
        if m:
            result["hmac"] = m.group(1)
        m = re.search(r'"relayState":"([^"]*)"', html)
        if m:
            result["relayState"] = m.group(1)
        return result

    @staticmethod
    def _extract_marketing_consent_fields(html: str) -> dict:
        """
        Extrahiert alle POST-Felder für .../consent/marketing/.../skip aus dem
        window._IDK.templateModel JSON-Objekt im HTML der Consent-Seite.
        Verifiziert anhand HAR-Aufzeichnung 18.06.2026.
        """
        result = {}
        m = re.search(r"csrf_token:\s*'([^']*)'", html)
        if m:
            result["_csrf"] = m.group(1)
        m = re.search(r'"documentKey":"([^"]*)"', html)
        if m:
            result["documentKey"] = m.group(1)
        m = re.search(r'"relayStateToken":"([^"]*)"', html)
        if m:
            result["relayState"] = m.group(1)
        m = re.search(r'"hmac":"([^"]*)"', html)
        if m:
            result["hmac"] = m.group(1)
        m = re.search(r'"countryOfJurisdiction":"([^"]*)"', html)
        if m:
            result["countryOfJurisdiction"] = m.group(1)
        m = re.search(r'"language":"([^"]*)"', html)
        if m:
            result["language"] = m.group(1)
        m = re.search(r'"callback":"(https://[^"]*)"', html)
        if m:
            result["callback"] = m.group(1)
        m = re.search(r'"step":"(\d+)"', html)
        if m:
            result["step"] = m.group(1)
        channel_map = {"email": "channelemail", "mail": "channelmail",
                       "phone": "channelphone", "app": "channelapp", "sms": "channelsms"}
        m = re.search(r'"marketChannels":\[(.*?)\]', html)
        if m:
            channels_raw = m.group(1)
            for channel_id, field_name in channel_map.items():
                cm = re.search(
                    rf'"channelId":"{channel_id}","channelType":"([^"]*)"', channels_raw
                )
                if cm:
                    result[field_name] = "true" if cm.group(1) != "NOT_USED" else "false"
        return result

    def _handle_marketing_consent_if_present(self, current_url: str, body: bytes) -> str:
        """
        Behandelt optionale Marketing-Consent-Seiten nach dem Login automatisch
        mit "Nicht jetzt". Muster (HAR 18.06.2026):
          POST .../consent/marketing/{user_id}/{client_id}/0/skip -> 200
          POST .../consent/marketing/{user_id}/{client_id}/1/skip -> 302
        Gibt die finale Redirect-URL zurück.
        """
        html = body.decode("utf-8", errors="replace")
        url = current_url.split("?")[0].rstrip("/") + "/skip"
        guard = 0
        while guard < 5:
            guard += 1
            fields = self._extract_marketing_consent_fields(html)
            missing = [k for k in ("_csrf", "documentKey", "relayState", "hmac",
                                    "countryOfJurisdiction", "language", "callback")
                       if k not in fields]
            if missing:
                raise CupraLoginError(
                    f"Marketing-Consent-Schritt {guard}: Felder nicht gefunden: {missing}"
                )
            logger.info(
                "Marketing-Consent-Zwischenschritt (Durchlauf %d, documentKey=%s) - "
                "lehne automatisch ab.", guard, fields["documentKey"]
            )
            status, headers, body = self._request(
                "POST", url,
                data={
                    "_csrf": fields["_csrf"],
                    "documentKey": fields["documentKey"],
                    "relayState": fields["relayState"],
                    "hmac": fields["hmac"],
                    "countryOfJurisdiction": fields["countryOfJurisdiction"],
                    "language": fields["language"],
                    "callback": fields["callback"].replace(" ", "%20"),
                    "channelemail": fields.get("channelemail", "false"),
                    "channelmail": fields.get("channelmail", "false"),
                    "channelphone": fields.get("channelphone", "false"),
                    "channelapp": fields.get("channelapp", "false"),
                    "channelsms": fields.get("channelsms", "false"),
                },
            )
            if status == 302:
                return headers["Location"]
            if status == 200:
                html = body.decode("utf-8", errors="replace")
                next_fields = self._extract_marketing_consent_fields(html)
                next_step = next_fields.get("step")
                if not next_step:
                    raise CupraLoginError(
                        f"Marketing-Consent-Durchlauf {guard}: 'step' nicht gefunden."
                    )
                url = re.sub(r'/\d+/skip$', f'/{next_step}/skip', url)
                continue
            raise CupraLoginError(
                f"Unerwarteter Status {status} bei Marketing-Consent-Durchlauf {guard}."
            )
        raise CupraLoginError("Marketing-Consent-Schleife nach 5 Durchläufen nicht beendet.")

    def _get_cookie(self, name: str):
        for cookie in self.cookie_jar:
            if cookie.name == name:
                return cookie.value
        return None

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    def is_logged_in(self) -> bool:
        if self._token_expires_at is None:
            return False
        if not self._get_cookie("access_token"):
            return False
        return time.time() < (self._token_expires_at - self.TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS)

    def ensure_logged_in(self) -> None:
        """Loggt ein falls nötig. Liest request_identifier automatisch aus dem
        Portal aus, falls er noch leer ist (via fetch_request_identifier)."""
        if self.is_logged_in():
            logger.debug("Token noch gültig, kein erneuter Login nötig.")
        else:
            logger.debug("Kein gültiger Token, Login wird durchgeführt.")
            self.login()
        if not self.request_identifier:
            self.request_identifier = self.fetch_request_identifier()

    def _retry_delays(self):
        for count, delay in self.RETRY_SCHEDULE:
            for _ in range(count):
                yield delay / self._retry_speedup
        while True:
            yield self.RETRY_FINAL_DELAY_SECONDS / self._retry_speedup

    def _with_retry(self, func, *args, **kwargs):
        attempt = 0
        for delay in self._retry_delays():
            attempt += 1
            try:
                return func(*args, **kwargs)
            except CupraPermanentError:
                raise
            except CupraRetryableError as e:
                logger.warning(
                    "Versuch %d fehlgeschlagen (%s) - erneuter Versuch in %ds.",
                    attempt, e, delay,
                )
                self._token_expires_at = None
                time.sleep(delay)

    def login(self) -> None:
        logger.info("Schritt 1/9: Authorize-Request")
        authorize_params = {
            "client_id": CLIENT_ID, "response_type": "code", "scope": SCOPE,
            "state": STATE, "redirect_uri": REDIRECT_URI, "prompt": "login",
        }
        url = f"{IDENTITY_BASE}/oidc/v1/authorize?{urllib.parse.urlencode(authorize_params)}"
        status, headers, _ = self._request("GET", url)
        if status != 302:
            raise CupraLoginError(f"Authorize fehlgeschlagen: HTTP {status}")
        signin_url = headers["Location"]

        logger.info("Schritt 2/9: Signin-Seite laden")
        status, headers, body = self._request("GET", signin_url)
        html = body.decode("utf-8")
        fields = self._extract_hidden_inputs(html)
        if not fields.get("_csrf"):
            raise CupraLoginError("CSRF-Token von Signin-Seite nicht extrahierbar.")

        logger.info("Schritt 3/9: E-Mail senden")
        post_url = signin_url.split("?")[0].replace("/signin/", "/") + "/login/identifier"
        status, headers, _ = self._request("POST", post_url, data={
            "_csrf": fields["_csrf"], "relayState": fields["relayState"],
            "hmac": fields["hmac"], "email": self.email,
        })
        if status != 303:
            raise CupraLoginError(f"E-Mail-Schritt fehlgeschlagen: HTTP {status}")
        authenticate_url = IDENTITY_BASE + headers["Location"]

        logger.info("Schritt 4/9: Passwort-Seite laden")
        status, headers, body = self._request("GET", authenticate_url)
        html = body.decode("utf-8")
        pw_fields = self._extract_js_model_fields(html)
        if not pw_fields.get("_csrf"):
            raise CupraLoginError("CSRF-Token von Passwort-Seite nicht extrahierbar.")

        logger.info("Schritt 5/9: Passwort senden")
        status, headers, body = self._request(
            "POST", authenticate_url.split("?")[0],
            data={
                "_csrf": pw_fields["_csrf"], "relayState": pw_fields["relayState"],
                "hmac": pw_fields["hmac"], "email": self.email, "password": self.password,
            },
        )
        if status == 303:
            location = headers.get("Location", "")
            if "error=" in location:
                error_match = re.search(r"error=([\w.]+)", location)
                raise CupraPermanentError(
                    f"Login abgelehnt: {error_match.group(1) if error_match else 'unbekannt'}"
                )
            raise CupraLoginError(f"Unerwarteter 303 ohne Fehler: {location}")
        if status != 302:
            raise CupraLoginError(f"Passwort-Schritt fehlgeschlagen: HTTP {status}")
        sso_url = headers["Location"]

        logger.info("Schritt 6/9: SSO-Redirect folgen")
        status, headers, body = self._request("GET", sso_url)
        if status == 200 and "/consent/marketing/" in sso_url:
            consent_url = self._handle_marketing_consent_if_present(sso_url, body)
        elif status != 302:
            raise CupraLoginError(f"SSO-Schritt fehlgeschlagen: HTTP {status}")
        else:
            consent_url = headers["Location"]

        logger.info("Schritt 7/9: Consent-Redirect folgen")
        status, headers, body = self._request("GET", consent_url)
        if status == 200 and "/consent/marketing/" in consent_url:
            callback_success_url = self._handle_marketing_consent_if_present(consent_url, body)
        elif status != 302:
            raise CupraLoginError(f"Consent-Schritt fehlgeschlagen: HTTP {status}")
        else:
            callback_success_url = headers["Location"]

        logger.info("Schritt 8/9: Callback/success -> Authorization Code")
        status, headers, _ = self._request("GET", callback_success_url)
        if status != 302:
            raise CupraLoginError(f"Callback-Schritt fehlgeschlagen: HTTP {status}")
        portal_login_url = headers["Location"]

        logger.info("Schritt 9/9: Code beim Portal einlösen")
        status, headers, _ = self._request("GET", portal_login_url)
        if status != 302:
            raise CupraLoginError(f"Portal-Login fehlgeschlagen: HTTP {status}")
        callbacklogin_url = headers["Location"]

        status, headers, _ = self._request("GET", callbacklogin_url)
        if status != 302:
            raise CupraLoginError(f"Portal-Callback fehlgeschlagen: HTTP {status}")

        if not self._get_cookie("access_token"):
            raise CupraLoginError("Login durchlaufen, aber kein access_token Cookie erhalten.")

        try:
            payload = self._decode_jwt_payload(self._get_cookie("access_token"))
            self._token_expires_at = payload["exp"]
            logger.debug(
                "Token gültig bis %s (in %d Sekunden)",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._token_expires_at)),
                self._token_expires_at - time.time(),
            )
        except Exception as e:
            logger.warning("Token-Ablaufzeit nicht auslesbar (%s) - Login beim nächsten Aufruf.", e)
            self._token_expires_at = None

        logger.info("Login erfolgreich.")

    def fetch_request_identifier(self) -> str:
        """
        Liest den Identifier der Daueranfrage automatisch aus dem Portal aus.
        Endpunkt: GET /proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial
        Verifiziert anhand HAR-Aufzeichnung 18.06.2026.
        Hinweis: /metadata/all liefert die one-time Anfrage - nicht verwenden.
        """
        # Direkt is_logged_in() statt ensure_logged_in() prüfen,
        # da ensure_logged_in() seinerseits fetch_request_identifier() aufruft.
        if not self.is_logged_in():
            self.login()
        url = f"{PORTAL_BASE}/proxy_api/euda-apim/datarequest/vehicles/{self.vin}/metadata/partial"
        status, headers, body = self._request("GET", url)
        if status != 200:
            raise CupraLoginError(f"Identifier konnte nicht ausgelesen werden: HTTP {status}")
        data = json.loads(body)
        identifier = data.get("Identifier")
        name = data.get("Name", "unbekannt")
        if not identifier:
            raise CupraLoginError(
                "Kein Identifier in der Portal-Antwort. "
                "Möglich: noch keine Daueranfrage (type=partial) im Portal angelegt."
            )
        logger.info("Daueranfrage gefunden: '%s' (Identifier: %s)", name, identifier)
        return identifier

    def list_files(self) -> list:
        return self._with_retry(self._list_files_once)

    def validate_credentials(self) -> None:
        self._list_files_once()

    def _list_files_once(self) -> list:
        self.ensure_logged_in()
        url = (
            f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
            f"{self.vin}/{self.request_identifier}/list"
        )
        status, headers, body = self._request("GET", url, headers={"type": "partial"})
        if status == 400:
            raise CupraPermanentError(
                f"Datei-Liste nicht geladen (VIN/Identifier prüfen): "
                f"{body[:300].decode('utf-8', errors='replace')}"
            )
        if status != 200:
            raise CupraLoginError(f"Datei-Liste nicht geladen: HTTP {status}")
        return json.loads(body)

    def download_latest(self):
        return self._with_retry(self._download_latest_once)

    def _download_latest_once(self):
        files = self._list_files_once()
        if not files:
            raise CupraLoginError("Keine Dateien verfügbar.")
        latest = sorted(files, key=lambda f: f["createdOn"], reverse=True)[0]
        logger.info("Neueste Datei: %s (%s Bytes)", latest["name"], latest.get("size"))
        url = (
            f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
            f"{self.vin}/{self.request_identifier}/download"
        )
        status, headers, body = self._request(
            "GET", url, headers={"type": "partial", "filename": latest["name"]},
        )
        if status != 200:
            raise CupraLoginError(f"Download fehlgeschlagen: HTTP {status}")
        return body, latest["name"]
