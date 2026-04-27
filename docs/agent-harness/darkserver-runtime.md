# DarkServer Runtime

DarkServer runs this Hermes fork as a containerized agent environment. This document records runtime assumptions for agent work.

## Boundaries

- Do not touch DarkLS.
- Do not use SSH to DarkServer.
- Do not edit runtime secrets or credentials unless explicitly asked.
- Ask before restarting the Hermes container when the active session may be killed.

## Paths

- Repository: `/app/code`
- Runtime home: `/root/.hermes`
- Preferred test Python: `/app/venv/bin/python3`
- DarkServer image: `Dockerfile.darkserver`
- Startup script: `scripts/darkserver-start.sh`

## Runtime state

Runtime state belongs outside the repo under `/root/.hermes`. Repo commits should not contain credentials, tokens, runtime databases, logs, or generated state.

## Operational posture

Use the container-local tools already available. For host/container management, use Portainer or Unraid APIs when available. Avoid host-level assumptions from memory unless verified in the live environment.
