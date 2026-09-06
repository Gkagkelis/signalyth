# SIGNALYTH v1.8.4.4 status-label fix

Found while verifying the v1.8.4.3 credential-storage hotfix (BREAK DELIBERATELY step of the quality gate):

- /api/integrations/status reported a hardcoded, incorrect label ("user-data/secrets.env") for
  where credentials are stored. The real location (set in v1.8.4.3) is ~/.signalyth/secrets.env.
- This was a display-only label mismatch. It did NOT affect where secrets were actually written or
  read from - the v1.8.4.3 storage fix itself was correct and is unchanged.
- Fixed: the status endpoint now reports the true, actual secrets file path.

Verification performed:
- Full automated suite: 507/507 tests passed (before and after this fix).
- Manual smoke test: launched the server fresh, confirmed home page loads, confirmed a saved
  Apify credential is written to the correct external path with correct permissions, and confirmed
  the status endpoint now shows that real path.
- Confirmed the app does not crash when an Apify connection test fails (graceful error, HTTP 409
  with a clear message) - this is the pattern you will see if a token is invalid or Apify is
  unreachable.

No other files changed in this hotfix beyond app/services/integrations.py.
