# Separation test build

This package is intended for local testing before publication.

Architecture:
- `router.py`: backend/application logic only; no embedded HTML and no human-readable Italian/English prose.
- `webui.html`: complete Web UI structure, CSS and JavaScript; visible UI text comes from the language packs.
- `lang_it.json`: Italian text.
- `lang_en.json`: English text.
- `gisell-router.service.template`: external systemd unit template.

The final ZIP is verified after creation by extracting it into a clean directory and checking Python syntax, Python 3.10 grammar compatibility, JavaScript syntax, JSON language-pack parity, language placeholders, absence of HTML in `router.py`, HTTP startup, IT/EN language endpoints, `/v1/models`, localized API errors, service-template rendering, and startup through `run.sh` using an isolated venv with system site packages.
