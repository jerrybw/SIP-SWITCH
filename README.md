# SIP Switch Gateway (M1 skeleton, reconstructed)

ESL listener -> CDR into MySQL(sip_switch) + FastAPI mgmt API.

Run:
    cd /opt/sip-switch-gateway
    python3 -m venv venv && . venv/bin/activate
    pip install -r requirements.txt
    python -m src.main

Endpoints:
    GET /health
    GET /cdrs?limit=50&hours=24

Note: reconstructed from PRD/schema because the netdrive presigned
download was blocked from this server. Overlay the official skeleton
(sip-switch-gateway/) when reachable.
