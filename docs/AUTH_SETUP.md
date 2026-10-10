# Private sharing with Google sign-in

Run the commands in this guide from the repository root.

For the supplied PW kit, the `Whitelisted` tab's `Lecture_Trimmer` email column,
and Desktop OAuth JSON, use [PW_DESKTOP_SETUP.md](PW_DESKTOP_SETUP.md) instead.
The instructions below are for a separate Sheet and a hosted HTTPS Web OAuth client.

The local app still starts with `python app.py --port 7861`. To share it with other people, host **one server** behind HTTPS and use the private access mode below. People open a browser link and sign in with their own Google account. Their email must be enabled in your private Google Sheet.

An email typed into a form is never accepted as proof of identity. Google verifies the account; the server verifies the signed Google ID token, audience, issuer, expiry, verified email and nonce. The Google account's stable subject ID becomes an opaque owner ID for files and jobs.

## 1. Prepare the access Sheet

Create a private Sheet with a tab named `Access` and these headers in row 1:

| email | enabled | expires_at |
|---|---|---|
| teacher@example.com | TRUE | |
| temporary@example.com | TRUE | 2027-01-01T00:00:00+05:30 |
| removed@example.com | FALSE | |

Use a Google Sheets checkbox or TRUE/FALSE for `enabled`. A blank value disables the account. A blank expiry means no scheduled expiry; a nonblank expiry must be an ISO timestamp with an explicit timezone. Format this column as **Plain text** so Sheets preserves the timezone. Date-only values are rejected rather than interpreted ambiguously.

The configured range must contain the header and all access rows. You can rename the headers using the environment settings below. Duplicate email rows, malformed values, missing headers and a Sheet API failure all deny access after the cached snapshot expires. Emails are matched case-insensitively; Gmail dots or plus suffixes are not rewritten.

## 2. Give the server read-only access

In a Google Cloud project, enable the **Google Sheets API**. Create a service account for the server, then share this one private Sheet with the service account's email as **Viewer**. Do not publish the Sheet or make it available to anyone with the link.

Use the hosting platform's service account / Application Default Credentials where available. For a server that needs a JSON credential file, save it outside this repository and the public web directories, restrict its filesystem permissions, and set `TRIMMER_GOOGLE_CREDENTIALS_FILE`. This file belongs only on the server; do not put it in an EXE or distribute it to users. The reader requests only `spreadsheets.readonly`.

## 3. Configure Google sign-in

Create a Google OAuth **Web application** client in the same or another Google Cloud project. Configure the consent screen and its intended users. Register this exact redirect URI, replacing the example hostname:

```text
https://lectures.example.com/auth/callback
```

Only the `openid email` scopes are requested from users; users do not authorize access to your Sheet. Sheet access is performed separately by the server service account. While the Google OAuth app is in testing, add the intended accounts as Google test users too.

## 4. Configure the server environment

Add these to the server's secret environment or protected `.env`. Do not commit real values:

```dotenv
TRIMMER_AUTH=google-sheet
TRIMMER_BASE_URL=https://lectures.example.com
TRIMMER_GOOGLE_CLIENT_ID=YOUR_CLIENT_ID.apps.googleusercontent.com
TRIMMER_GOOGLE_CLIENT_SECRET=YOUR_CLIENT_SECRET
TRIMMER_ACCESS_SHEET_ID=ID_FROM_THE_SHEET_URL
TRIMMER_ACCESS_SHEET_RANGE=Access!A1:C10000
TRIMMER_ACCESS_EMAIL_COLUMN=email
TRIMMER_ACCESS_ENABLED_COLUMN=enabled
TRIMMER_ACCESS_EXPIRES_COLUMN=expires_at
TRIMMER_ACCESS_REFRESH_SECONDS=60
TRIMMER_SESSION_SECONDS=28800
# Optional when Application Default Credentials are unavailable:
TRIMMER_GOOGLE_CREDENTIALS_FILE=C:/server-secrets/trimmer-service-account.json
# Trust only the actual reverse proxy; never use * on an exposed server:
TRIMMER_TRUSTED_PROXY_IPS=127.0.0.1
```

`TRIMMER_BASE_URL` must be an HTTPS origin with no path. Configure your HTTPS reverse proxy to forward that host to the app port and permit large uploads (up to the app's 5 GB limit) and long-lived event streams. Disable proxy buffering for `/api/events` (live progress). Bind the app to loopback when the proxy is on the same server:

```powershell
python app.py --auth google-sheet --host 127.0.0.1 --port 7861 --no-browser
```

Use **one worker**. Sessions are intentionally in memory; restarting signs everyone out (their lectures and queue are kept). Shared passwords are not used for this multi-user mode because they do not supply per-user isolation.

## Access and privacy behavior

- Secure, HttpOnly, SameSite cookies carry random opaque tokens. OAuth uses single-use, five-minute state and nonce values plus PKCE. Access tokens and ID tokens are not kept in browser cookies.
- All app, upload, video, report and queue routes require a verified session. Exact file grants prevent one signed-in account from downloading another account's files, including when the URL is known. Queue sessions are bound to an owner too.
- Shared callbacks accept only files uploaded by that signed-in account. Local paths and local job-history controls are disabled. The processing pipeline records the opaque owner ID in each job report.
- Disabling/removing a user takes effect on new requests after the access snapshot refreshes (60 seconds by default; configurable 15–300 seconds). Expiry is checked on every request. Long-running streams recheck during data delivery; a few seconds or the next stream heartbeat may be needed. Already downloaded files cannot be revoked.
- If Google Sheets is unavailable after a snapshot expires, access is denied until a successful refresh. There is no fallback to a stale allowlist or password bypass.
- Sign out invalidates the server session. A server restart invalidates every session and in-memory file grant. Existing job files remain on server storage for the administrator; users cannot browse that storage.
- The application still uses the configured speech-recognition and classification providers. Review their data handling for your deployment and tell users which providers receive lecture data. Visual review is an explicit upload-time option.

## Verification before inviting users

The automated suite uses simulated Google identities and the real studio API to test anonymous denial, two-user isolation of lectures and files, upload ownership, origin checks, expired/disabled access, invalid configuration, nonce/state replay and failure when the Sheet is unavailable.

Live OAuth and live Sheet access require your real domain and credentials and are not asserted by these tests. Before inviting users, verify sign-in and logout with two accounts, reject an unlisted account, disable a test account in the Sheet and confirm access is removed, and confirm one account cannot open the other's video/report link. Then test an actual upload/export behind your reverse proxy.

Verify the configured transcription and classification providers' **daily quota, request-rate limits, billing status and spend controls** before accepting other users' lectures. A model can be available yet have insufficient daily capacity for a long lecture. The app cannot remove a provider's quota limit or promise a completion time while that limit is exhausted. Test a representative long lecture and budget for analysis, optional visual review and retries. Do not repeatedly force fresh analysis while waiting for quota; saved successful analysis work can be reused on retry. Saved results display their analysis version so older videos are not mistaken for newly generated output.

This implementation is a single-server deployment foundation. Horizontal scaling needs shared session/ownership storage, a shared queue, storage retention rules and operational monitoring before adding workers or replicas.

References: [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect), [Google service accounts](https://developers.google.com/identity/protocols/oauth2/service-account), [Sheets values API](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get).
