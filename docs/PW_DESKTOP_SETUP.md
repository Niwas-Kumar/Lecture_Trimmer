# PW Google sign-in for Lecture Trimmer

Run the commands in this guide from the repository root.

This mode uses the Desktop OAuth JSON you created, the supplied `vendor/pw_access.py`,
and the PW proxy's private access Sheet. It runs on your computer at
`http://127.0.0.1:7861`. The existing HTTPS `google-sheet` hosting mode is separate.

## 1. Add approved users to the existing PW Sheet

Open the [PW control Sheet](https://docs.google.com/spreadsheets/d/1aaF3y0VsgyB_YcyfDK33VWcCagzwxBPOwjoe2TvfbHE/edit?gid=803879933#gid=803879933).
On the **Whitelisted** tab, the row-one header must be exactly `Lecture_Trimmer`.
Enter each approved `@pw.live` email below it, one email per cell. This column
contains emails, not TRUE/FALSE flags. Keep the Sheet private.

The PW proxy reads this Sheet. A Desktop OAuth JSON does not itself grant Sheet
access. No service-account key or Sheets sharing permission is needed on each
user's computer. If an approved account is rejected, the PW administrator must
check the proxy's access to this Sheet and the exact app header.

## 2. Finish the Google Cloud settings

Select the Cloud project that owns your downloaded OAuth JSON.

1. Under **Google Auth platform → Branding**, confirm the app name and support email.
2. Under **Audience**, use Internal for the appropriate Workspace organization,
   or External for the external setup. If External is in Testing, add your
   intended test accounts under **Test users**.
3. Under **Data Access**, configure only identity scopes: `openid` and
   `https://www.googleapis.com/auth/userinfo.email`. Users do not authorize access
   to the private Sheet.
4. Under **Clients**, confirm the client type is **Desktop app**. The app uses
   a loopback callback and PKCE. Do not substitute a Web application or service-account JSON.

The new OAuth client must be compatible with the PW proxy's Google-token
verification. If Google login succeeds but PW authorization fails, ask the PW
administrator to check the client ID/audience and proxy configuration.

## 3. Configure and start

Save the downloaded JSON in the project folder. These files are excluded from
Git and Docker builds and are never served by the app. Do not paste their
contents into chat or include them in source-control commits.

Add to the local `.env`, using the actual downloaded filename:

```dotenv
TRIMMER_AUTH=pw-desktop
TRIMMER_DESKTOP_OAUTH_FILE=client_secret_YOUR_DESKTOP_CLIENT.json
```

If the file setting is omitted, exactly one `client_secret*.json` in the project
folder is discovered automatically. Ambiguous or invalid credentials stop startup.

```powershell
python app.py --auth pw-desktop --host 127.0.0.1 --port 7861
```

Open `http://127.0.0.1:7861`. Click **Sign in with Google in your browser**.
Chrome, Edge, or your default browser opens. Complete sign-in there using an
approved `@pw.live` account. Keep using that browser for Lecture Trimmer; an
embedded browser tab has its own cookies and will not automatically become signed in.

After Google verifies your identity and PW grants access, you reach the lecture
upload screen. Upload a lecture and create the desired videos as before.

## What is connected

- Google ID tokens are checked for signature, issuer, audience, verified email,
  expiry, nonce, and the `pw.live` domain. OAuth state is single-use; PKCE protects
  the authorization-code exchange.
- Each login has an independent PW adapter instance and proxy-token cache. Tokens
  remain in memory; cookies hold only opaque session identifiers. Restarting or
  signing out requires another login. Desktop sessions last up to eight hours.
- The PW allowlist is checked at login and before each new lecture. While signed
  in, this client rechecks at most every 60 seconds on requests. The actual delay
  for a Sheet edit also depends on the proxy's own cache, which is not controlled
  here. Failed refreshes deny access; expired cached grants are never reused.
- Gemini transcript and optional image requests use the supplied PW adapter,
  including its media-storage handling. Every job has one task ID; each provider
  request records count 1 with the unit `No. of requests`. The proxy owns billing
  credentials and usage logging. There is no direct Gemini-key fallback in this mode.
- Uploads, outputs, reports, and queue events remain tied to the signed-in account.
  File browsing and old local job history are disabled in this authenticated mode.
- Speech recognition uses Groq Whisper with the owner's key packaged in the installer
  (or `GROQ_API_KEY` on this computer, which takes precedence);
  the displayed PW total covers classification only.

## First live checks

Use **Check PW connection** at the top of the signed-in app before uploading a
long lecture. It first sends a tiny text request, then checks the lecture JSON
format with one synthetic teaching sentence. It uses the configured model and
stops at the first failure. Safe reports are saved under `temp/pw_diagnostics/`
with HTTP status, error category, task ID, and a proxy request ID when supplied.
They contain no lecture, Google token, or provider key. Error categories are
diagnostic hints; the proxy's backend logs establish the underlying cause.

An HTTP 500 on the basic text check means the request already fails without
lecture media or the classification schema. Give the report's request ID to the
PW proxy administrator and inspect `/api/gemini/generate` runtime logs. A separate
direct Google HTTP 503 indicates an unavailable provider request, but does not
prove the proxy's HTTP 500 has the same cause. Retry a short check after a delay;
do not repeatedly restart full lecture processing while the check fails.

1. Sign in with an email present in `Lecture_Trimmer`; verify the upload screen opens.
2. Try an unlisted test account; verify access is denied.
3. Remove a test account from the column, wait for the proxy's access cache to
   refresh, then try starting a new lecture. Verify denial.
4. Sign out and verify private video/report links require login.
5. Run one short lecture with **Analyse only** first, then a short three-output
   export. Confirm its calls appear in PW's usage records.

Local automated tests use simulated Google and PW responses. They do not prove
that the live proxy accepts the new OAuth client, that a particular Sheet row is
approved, or that the proxy has quota/model access. A real login and model request
are required for those checks.

If Google shows `access_denied`, check the Audience/test-user settings and Workspace
administrator restrictions. If it shows `redirect_uri_mismatch`, check that the
loaded JSON is a Desktop client. If PW denies access, check the exact email,
column name, and proxy administrator configuration. Keep the local service running
throughout sign-in so the loopback callback can complete.

## Desktop EXE status

The desktop installer includes the runtime, FFmpeg and UI assets. Application
updates are delivered as signed packages. See
`DESKTOP_RELEASE_GUIDE.txt` for signed updates and distribution. Do not distribute
your `.env`, provider keys, service-account keys, saved sessions, or lecture files.

The installer packages the owner's Groq key by the owner's decision; see DESKTOP_RELEASE_GUIDE.txt.
Desktop OAuth client identifiers are not a security boundary. Access to paid AI
must continue to be enforced by the PW proxy. A modified local program cannot be
prevented from using its own local FFmpeg.

References: [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[Google consent settings](https://developers.google.com/workspace/guides/configure-oauth-consent).
