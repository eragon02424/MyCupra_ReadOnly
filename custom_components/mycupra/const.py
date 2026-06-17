"""Konstanten für die MyCupra (Read-Only) Integration."""

DOMAIN = "mycupra"

CONF_VIN = "vin"
CONF_DEVICE_NAME = "device_name"
CONF_REQUEST_IDENTIFIER = "request_identifier"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"

DEFAULT_DEVICE_NAME = "Tavascan"
DEFAULT_UPDATE_INTERVAL_MINUTES = 15

# "Home Assistant" Daueranfrage im EU Data Act Portal - liefert alle 15 Minuten
# eine neue ZIP-Datei. Diese Identifier-ID bleibt über alle Generierungen hinweg
# gleich; nur der Dateiname (z.B. 20260617151005_VIN.zip) ändert sich pro
# Generierung. Default-Wert für Jonathans Setup, im Config-Flow überschreibbar
# (z.B. für ein anderes Fahrzeug mit einer eigenen Daueranfrage im Portal).
DEFAULT_REQUEST_IDENTIFIER = "6s1d9sz06nzg7hbkpvg5z11p9q29u18s"
