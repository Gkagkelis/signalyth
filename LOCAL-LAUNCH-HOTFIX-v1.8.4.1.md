# SIGNALYTH v1.8.4.1 local-launch hotfix

Fixes the first real Mac local-launch defect discovered on macOS Big Sur / Python 3.11.9:

- `/` returned HTTP 500 because `Jinja2Templates.TemplateResponse` was called with the legacy positional signature while an installed Starlette/FastAPI stack interpreted the arguments using the newer request-first API.
- Home rendering now uses explicit keyword arguments (`request=`, `name=`, `context=`), avoiding positional-signature ambiguity.
- The FastAPI/Starlette/Uvicorn web stack is pinned to versions used for regression verification so a future `pip install` does not silently pull an incompatible web stack.
- Added a regression test for this exact failure.

This is a local-launch hotfix. It does not change collection, analysis, resilience, or actor orchestration logic.
