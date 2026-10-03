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
import uuid

logger = logging.getLogger(__name__)

# Sicherheitsnetz: Auth-Codes, Tokens, CSRF-/relayState-Werte aus URLs nie ins Log schreiben.
_SENSITIVE = re.compile(
    r"((?:code|relayState|state|hmac|token|access_token|id_token|_csrf|nonce)=)[^&\s\"']+",
    re.IGNORECASE,
)


class _RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        redacted = _SENSITIVE.sub(r"\1***", msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


logger.addFilter(_RedactFilter())


def _safe(url):
    """URL ohne Query-String (für Logausgaben)."""
    return str(url).split("?")[0]


def _page_title(body):
    """<title> einer HTML-Antwort (gekürzt) - nur zur Diagnose in Meldungen."""
    try:
        html = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:80]

CLIENT_ID = "f85e5b69-e3b2-43aa-9c0d-1b7d0e0b576f@apps_vw-dilab_com"
SCOPE = "openid cars profile"
STATE = "de__en__CUPRA"
REDIRECT_URI = "https://eu-data-act.drivesomethinggreater.com/login"
IDENTITY_BASE = "https://identity.vwgroup.io"
PORTAL_BASE = "https://eu-data-act.drivesomethinggreater.com"
DEFAULT_REQUEST_IDENTIFIER = ""
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


def authorize_url():
    """Start-URL der VW-Anmeldung (Schritt 1 des Logins, enthält keine Geheimnisse).

    Im Browser geöffnet führt sie durch Anmeldung und ggf. Zustimmungsseiten und endet
    wieder im Portal. Wird auch in der Home-Assistant-Meldung als Link angezeigt.
    """
    params = {"client_id": CLIENT_ID, "response_type": "code", "scope": SCOPE,
              "state": STATE, "redirect_uri": REDIRECT_URI, "prompt": "login"}
    return f"{IDENTITY_BASE}/oidc/v1/authorize?{urllib.parse.urlencode(params)}"


class CupraLoginError(Exception):
    pass

class CupraRetryableError(CupraLoginError):
    pass

class CupraPermanentError(CupraLoginError):
    pass

class CupraActionRequired(CupraPermanentError):
    """VW zeigt im Login eine Seite, die nur der Nutzer bestätigen kann
    (Zustimmung, geänderte Nutzungs-/Datenschutzbedingungen o. ä.).

    Das Add-on bestätigt solche Seiten bewusst NICHT automatisch. Es bricht ab,
    der Coordinator meldet den Fehler in Home Assistant, und der Nutzer meldet sich
    einmal selbst im Browser an und bestätigt (Link: authorize_url()).
    """

    def __init__(self, message, step=None, page=None, title=None):
        super().__init__(message)
        self.step = step
        self.page = page
        self.title = title

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CupraClient:
    TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60
    REQUEST_TIMEOUT_SECONDS = 15
    # Pro Aktualisierungslauf nur wenige Versuche. Der Coordinator versucht es im
    # nächsten Intervall (15 min) erneut. Vorher: endlose Retry-Schleife (bis 3600 s
    # Wartezeit), die den Setup/Start von Home Assistant blockieren konnte.
    MAX_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 10

    def __init__(self, email, password, vin, request_identifier=DEFAULT_REQUEST_IDENTIFIER, retry_speedup=1.0):
        self.email = email
        self.password = password
        self.vin = vin
        self.request_identifier = request_identifier
        self.cookie_jar = http.cookiejar.CookieJar()
        # Opener erst beim ersten Request (im Executor-Thread) anlegen: build_opener
        # lädt SSL-Zertifikate und darf nicht im Event-Loop laufen.
        self._opener = None
        self._token_expires_at = None
        self._retry_speedup = retry_speedup

    @property
    def opener(self):
        if self._opener is None:
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self.cookie_jar),
                NoRedirectHandler(),
            )
        return self._opener

    def _request(self, method, url, data=None, headers=None, allow_404=False):
        req_headers = {"User-Agent": USER_AGENT}
        if headers:
            req_headers.update(headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        started = time.monotonic()
        try:
            resp = self.opener.open(req, timeout=self.REQUEST_TIMEOUT_SECONDS)
            content = resp.read()
            logger.debug("HTTP %s %s -> %s (%d Bytes, %d ms)", method, _safe(url),
                         resp.status, len(content), (time.monotonic() - started) * 1000)
            return resp.status, resp.headers, content
        except urllib.error.HTTPError as e:
            status = e.code
            content = e.read()
            elapsed_ms = (time.monotonic() - started) * 1000
            if status in (301, 302, 303, 307, 308):
                logger.debug("HTTP %s %s -> %s Redirect (%d ms)", method, _safe(url), status, elapsed_ms)
                return status, e.headers, content
            if allow_404:
                logger.debug("HTTP %s %s -> %s (%d Bytes, %d ms, toleriert)", method, _safe(url),
                             status, len(content), elapsed_ms)
                return status, e.headers, content
            error_text = content[:300].decode('utf-8', errors='replace')
            logger.warning("HTTP %s %s -> %s (%d ms): %s", method, _safe(url), status,
                           elapsed_ms, error_text[:200])
            if status == 401:
                raise CupraRetryableError(f"HTTP 401 bei {_safe(url)}: {error_text}")
            raise CupraLoginError(f"HTTP {status} bei {_safe(url)}: {error_text}")
        except urllib.error.URLError as e:
            logger.warning("Netzwerkfehler %s %s nach %d ms: %s", method, _safe(url),
                           (time.monotonic() - started) * 1000, e.reason)
            raise CupraRetryableError(f"Netzwerkfehler bei {_safe(url)}: {e.reason}")
        except (TimeoutError, OSError) as e:
            logger.warning("Timeout/OS-Fehler %s %s nach %d ms: %r", method, _safe(url),
                           (time.monotonic() - started) * 1000, e)
            raise CupraRetryableError(f"Timeout/OS-Fehler bei {_safe(url)}: {e!r}")

    @staticmethod
    def _extract_hidden_inputs(html):
        result = {}
        for name in ("_csrf", "relayState", "hmac"):
            m = re.search(rf'name="{name}"\s+value="([^"]*)"', html)
            if m:
                result[name] = m.group(1)
        return result

    @staticmethod
    def _extract_js_model_fields(html):
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

    def _unexpected_step(self, step, status, url, body):
        """Login-Schritt lieferte keine Weiterleitung.

        HTTP 200 = VW zeigt eine Seite (Zustimmung, geänderte Bedingungen ...), die nur
        der Nutzer bestätigen kann -> CupraActionRequired (nichts wird automatisch
        bestätigt, kein Retry). Alles andere bleibt ein normaler Login-Fehler.
        """
        page = _safe(url)
        if status == 200:
            title = _page_title(body)
            logger.error(
                "Login-Schritt %s/9: VW zeigt eine Seite statt einer Weiterleitung (%s, Titel: %r) - "
                "Bestätigung durch den Nutzer nötig, es wird nichts automatisch bestätigt.",
                step, page, title,
            )
            raise CupraActionRequired(
                f"Login-Schritt {step}: VW verlangt eine Bestätigung im Browser ({page}, Titel: {title!r})",
                step=step, page=page, title=title,
            )
        raise CupraLoginError(f"Login-Schritt {step} fehlgeschlagen: HTTP {status} bei {page}")

    def _get_cookie(self, name):
        for cookie in self.cookie_jar:
            if cookie.name == name:
                return cookie.value
        return None

    @staticmethod
    def _decode_jwt_payload(token):
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    def is_logged_in(self):
        if self._token_expires_at is None:
            return False
        if not self._get_cookie("access_token"):
            return False
        return time.time() < (self._token_expires_at - self.TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS)

    def ensure_logged_in(self):
        if self.is_logged_in():
            logger.debug("Token noch gültig.")
        else:
            self.login()
        if not self.request_identifier:
            self.request_identifier = self.fetch_request_identifier()

    def _with_retry(self, func, *args, **kwargs):
        """Führt func mit wenigen Wiederholungen aus (nur bei CupraRetryableError)."""
        delay = self.RETRY_DELAY_SECONDS / self._retry_speedup
        last_error = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return func(*args, **kwargs)
            except CupraPermanentError:
                raise
            except CupraRetryableError as e:
                last_error = e
                self._token_expires_at = None
                if attempt < self.MAX_ATTEMPTS:
                    logger.warning("Versuch %d/%d fehlgeschlagen (%s) - Retry in %.0f s.",
                                   attempt, self.MAX_ATTEMPTS, e, delay)
                    time.sleep(delay)
                else:
                    logger.warning("Versuch %d/%d fehlgeschlagen (%s) - gebe für diesen Lauf auf.",
                                   attempt, self.MAX_ATTEMPTS, e)
        raise CupraLoginError(f"Nach {self.MAX_ATTEMPTS} Versuchen fehlgeschlagen: {last_error}")

    def login(self):
        logger.info("Schritt 1/9: Authorize-Request")
        url = authorize_url()
        status, headers, _ = self._request("GET", url)
        logger.debug("1/9 GET %s -> HTTP %s", _safe(url), status)
        if status != 302:
            raise CupraLoginError(f"Authorize fehlgeschlagen: HTTP {status}")
        signin_url = headers["Location"]
        logger.debug("1/9 Location: %s", _safe(signin_url))

        logger.info("Schritt 2/9: Signin-Seite laden")
        status, headers, body = self._request("GET", signin_url)
        logger.debug("2/9 GET %s -> HTTP %s (%d Bytes)", _safe(signin_url), status, len(body))
        fields = self._extract_hidden_inputs(body.decode("utf-8"))
        logger.debug("2/9 Extrahierte Felder: %s", list(fields.keys()))
        if not fields.get("_csrf"):
            raise CupraLoginError("CSRF nicht extrahierbar (Signin).")

        logger.info("Schritt 3/9: E-Mail senden")
        post_url = signin_url.split("?")[0].replace("/signin/", "/") + "/login/identifier"
        status, headers, _ = self._request("POST", post_url, data={
            "_csrf": fields["_csrf"], "relayState": fields["relayState"],
            "hmac": fields["hmac"], "email": self.email,
        })
        logger.debug("3/9 POST %s -> HTTP %s", post_url, status)
        if status != 303:
            raise CupraLoginError(f"E-Mail-Schritt fehlgeschlagen: HTTP {status}")
        authenticate_url = IDENTITY_BASE + headers["Location"]
        logger.debug("3/9 Location: %s", _safe(authenticate_url))

        logger.info("Schritt 4/9: Passwort-Seite laden")
        status, headers, body = self._request("GET", authenticate_url)
        logger.debug("4/9 GET %s -> HTTP %s (%d Bytes)", _safe(authenticate_url), status, len(body))
        pw_fields = self._extract_js_model_fields(body.decode("utf-8"))
        logger.debug("4/9 Extrahierte Felder: %s", list(pw_fields.keys()))
        if not pw_fields.get("_csrf"):
            raise CupraLoginError("CSRF nicht extrahierbar (Passwort).")

        logger.info("Schritt 5/9: Passwort senden")
        status, headers, body = self._request("POST", authenticate_url.split("?")[0], data={
            "_csrf": pw_fields["_csrf"], "relayState": pw_fields["relayState"],
            "hmac": pw_fields["hmac"], "email": self.email, "password": self.password,
        })
        logger.debug("5/9 POST %s -> HTTP %s", authenticate_url.split("?")[0], status)
        if status == 303:
            loc = headers.get("Location", "")
            logger.debug("5/9 303-Location: %s", _safe(loc))
            if "error=" in loc:
                m = re.search(r"error=([\w.]+)", loc)
                raise CupraPermanentError(f"Login abgelehnt: {m.group(1) if m else 'unbekannt'}")
            raise CupraLoginError(f"Unerwarteter 303: {loc}")
        if status != 302:
            self._unexpected_step(5, status, authenticate_url, body)
        sso_url = headers["Location"]
        logger.debug("5/9 Location: %s", _safe(sso_url))

        logger.info("Schritt 6/9: SSO-Redirect folgen")
        status, headers, body = self._request("GET", sso_url)
        logger.debug("6/9 GET %s -> HTTP %s (%d Bytes)", _safe(sso_url), status, len(body))
        if status != 302:
            self._unexpected_step(6, status, sso_url, body)
        consent_url = headers["Location"]
        logger.debug("6/9 Location: %s", _safe(consent_url))

        logger.info("Schritt 7/9: Consent-Redirect folgen")
        status, headers, body = self._request("GET", consent_url)
        logger.debug("7/9 GET %s -> HTTP %s (%d Bytes)", _safe(consent_url), status, len(body))
        if status != 302:
            self._unexpected_step(7, status, consent_url, body)
        callback_success_url = headers["Location"]
        logger.debug("7/9 Location: %s", _safe(callback_success_url))

        logger.info("Schritt 8/9: Callback/success -> Authorization Code")
        status, headers, body = self._request("GET", callback_success_url)
        logger.debug("8/9 GET %s -> HTTP %s", _safe(callback_success_url), status)
        if status != 302:
            self._unexpected_step(8, status, callback_success_url, body)
        portal_login_url = headers["Location"]
        logger.debug("8/9 Location: %s", _safe(portal_login_url))

        logger.info("Schritt 9/9: Code beim Portal einlösen")
        status, headers, body = self._request("GET", portal_login_url)
        logger.debug("9/9 GET %s -> HTTP %s", _safe(portal_login_url), status)
        if status != 302:
            self._unexpected_step(9, status, portal_login_url, body)
        callback_login_url = headers["Location"]
        status, headers, body = self._request("GET", callback_login_url)
        logger.debug("9/9 GET %s -> HTTP %s", _safe(callback_login_url), status)
        if status != 302:
            self._unexpected_step(9, status, callback_login_url, body)
        logger.debug("9/9 Finale Location: %s", _safe(headers.get("Location", "")))

        if not self._get_cookie("access_token"):
            raise CupraLoginError("Kein access_token nach Login erhalten.")

        try:
            payload = self._decode_jwt_payload(self._get_cookie("access_token"))
            self._token_expires_at = payload["exp"]
            logger.debug("Token gültig bis %s",
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._token_expires_at)))
        except Exception as e:
            logger.warning("Token-Ablaufzeit nicht auslesbar: %s", e)
            self._token_expires_at = None

        logger.info("Login erfolgreich.")

    def fetch_vins(self):
        """
        Liest alle Fahrzeug-VINs des eingeloggten Accounts aus dem Portal.
        Endpunkt: GET /proxy_api/vum/v2/users/me/relations
        Erfordert traceId-Header (zufällige UUID), verifiziert 18.06.2026.
        Antwortformat: {"relations": [{"vehicle": {"vin": "..."}}, ...]}
        """
        if not self.is_logged_in():
            self.login()
        url = f"{PORTAL_BASE}/proxy_api/vum/v2/users/me/relations"
        status, headers, body = self._request(
            "GET", url, headers={"traceId": str(uuid.uuid4())}, allow_404=True
        )
        if status == 200:
            data = json.loads(body)
            relations = data.get("relations", [])
            vins = [r["vehicle"]["vin"] for r in relations
                    if isinstance(r.get("vehicle"), dict) and r["vehicle"].get("vin")]
            if vins:
                logger.info("Fahrzeuge gefunden: %s", vins)
                return vins
        raise CupraLoginError(
            f"VIN-Liste nicht auslesbar (Status {status}). "
            "Bitte prüfen ob das Fahrzeug im EU Data Act Portal registriert ist."
        )

    def fetch_request_identifier(self):
        """
        Liest den Identifier der Daueranfrage (type=partial) aus dem Portal.
        Endpunkt: GET /proxy_api/euda-apim/datarequest/vehicles/{VIN}/metadata/partial
        Verifiziert anhand HAR 18.06.2026.
        """
        if not self.is_logged_in():
            self.login()
        url = f"{PORTAL_BASE}/proxy_api/euda-apim/datarequest/vehicles/{self.vin}/metadata/partial"
        status, headers, body = self._request("GET", url)
        if status != 200:
            raise CupraLoginError(f"Identifier nicht auslesbar: HTTP {status}")
        data = json.loads(body)
        identifier = data.get("Identifier")
        if not identifier:
            raise CupraLoginError("Kein Identifier - Daueranfrage im Portal anlegen.")
        logger.info("Daueranfrage: '%s' (Identifier: %s)", data.get("Name", "?"), identifier)
        return identifier

    def list_files(self):
        """Liste aller aktuell im Portal vorhandenen Dateien (mit Retry)."""
        return self._with_retry(self._list_files_once)

    def validate_credentials(self):
        self._list_files_once()

    def _list_files_once(self):
        self.ensure_logged_in()
        url = (f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
               f"{self.vin}/{self.request_identifier}/list")
        status, headers, body = self._request("GET", url, headers={"type": "partial"})
        if status == 400:
            raise CupraPermanentError(f"VIN/Identifier prüfen: {body[:200].decode('utf-8', errors='replace')}")
        if status != 200:
            raise CupraLoginError(f"Dateiliste fehlgeschlagen: HTTP {status}")
        files = json.loads(body)
        logger.debug("Dateiliste: %d Einträge", len(files))
        return files

    def download_file(self, filename):
        """Lädt genau eine Datei (mit Retry)."""
        return self._with_retry(self._download_file_once, filename)

    def _download_file_once(self, filename):
        self.ensure_logged_in()
        url = (f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
               f"{self.vin}/{self.request_identifier}/download")
        status, headers, body = self._request(
            "GET", url, headers={"type": "partial", "filename": filename},
        )
        if status != 200:
            raise CupraLoginError(f"Download {filename} fehlgeschlagen: HTTP {status}")
        logger.debug("Datei %s geladen (%d Bytes)", filename, len(body))
        return body

    def download_latest(self):
        return self._with_retry(self._download_latest_once)

    def _download_latest_once(self):
        files = self._list_files_once()
        if not files:
            raise CupraLoginError("Keine Dateien verfügbar.")
        latest = sorted(files, key=lambda f: f["createdOn"], reverse=True)[0]
        logger.info("Neueste Datei: %s (%s Bytes)", latest["name"], latest.get("size"))
        url = (f"{PORTAL_BASE}/proxy_api/euda-apim/datadelivery/vehicles/"
               f"{self.vin}/{self.request_identifier}/download")
        status, headers, body = self._request(
            "GET", url, headers={"type": "partial", "filename": latest["name"]},
        )
        if status != 200:
            raise CupraLoginError(f"Download fehlgeschlagen: HTTP {status}")
        return body, latest["name"]
