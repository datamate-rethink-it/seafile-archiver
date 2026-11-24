"""Archivierung der Bibliotheken deaktivierter Seafile-User über seafobj (Seafiles eigene Objekt-Bibliothek)."""
import os

# Wird beim Bau des Images gesetzt (Git-Tag), siehe Dockerfile
__version__ = os.environ.get("SEAFILE_ARCHIVER_VERSION") or "dev"
