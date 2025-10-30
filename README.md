## Traffic Light – Minimal

Run (from this directory):
```bash
python3 traffic_light_single.py
```

Web UI: `http://<pi-ip>:8000`

Optional env vars: `DB_CLIENT_ID`, `DB_CLIENT_SECRET`, `OWM_API_KEY`, `GOOGLE_MAPS_API_KEY`.

Add mode: edit `traffic_light_single.py` (create `handle_<name>_mode` + register in `mode_handlers`).

Stop: Ctrl+C.

Done.
