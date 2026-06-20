# Local Nitter for paper-watch

paper-watch's Twitter source (`src/paper_watch/sources/twitter_nitter.py`) reads
per-handle RSS from a Nitter instance at `{instance}/{handle}/rss`. Public Nitter
instances are effectively dead since X disabled guest accounts (Feb 2024), so we run
our own here, bound to `http://localhost:8080`.

Self-hosted Nitter needs **session tokens from real X accounts**. We use throwaway
accounts and let Nitter rotate across all of them.

## Layout

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Nitter + bundled Redis, bound to `127.0.0.1:8080` |
| `nitter.conf` | Nitter config (RSS enabled, points at the bundled Redis) |
| `get_session.py` | Vendored from [zedeus/nitter](https://github.com/zedeus/nitter) `tools/`; scripted API login |
| `gen-session.sh` | Wrapper: runs `get_session.py` via `uv` for one account |
| `add_cookie_session.py` | Build a session from cookies you copy out of a browser (fallback) |
| `add-session.sh` | Wrapper for `add_cookie_session.py` |
| `sessions.jsonl` | Generated session tokens, one line per account. **Gitignored — secret.** |

## 1. Generate sessions (one per throwaway account)

```bash
cd deploy/nitter
./gen-session.sh <username>
```

It prompts (hidden) for the password and 2FA secret and passes them via env vars,
not argv, so they never show up in `ps`/`/proc`. Leave the 2FA prompt blank if the
account has no TOTP 2FA. Repeat for each account; each run appends a line to
`sessions.jsonl` (created `0600`). More accounts = higher effective rate limit and
resilience when one gets flagged.

Dependencies are pinned and hash-locked in `requirements-session.txt` (they handle
credentials, so they shouldn't float); regenerate it with
`uv pip compile requirements-session.in --generate-hashes -o requirements-session.txt`.

### 1b. Fallback: cookies from your browser (when the scripted login fails)

X often blocks the scripted login with a captcha or new-device verification that
`get_session.py` can't clear. In that case, grab the session cookies from a browser
instead — no Chromium or automation needed:

1. Open a **Firefox Private Window** (`Ctrl+Shift+P`). A private window has its own
   cookie jar, so signing in here **does not disturb the X account you're logged into
   in normal windows** — that's the isolation that keeps this from colliding with your
   existing sessions. (A separate Firefox profile via `firefox -P` works too.)
2. Go to `https://x.com` and log in as the throwaway account.
3. Open DevTools (`F12`) → **Storage** tab → **Cookies** → `https://x.com`.
4. Copy the values of three cookies: `auth_token`, `ct0`, and `twid`.
5. Run the helper and paste each when prompted (input is hidden):
   ```bash
   ./add-session.sh <username>
   ```
6. Close the private window (you can leave the account logged in there or not — the
   captured tokens stay valid in `sessions.jsonl`). Restart Nitter.

Repeat per throwaway account. Do **not** log the throwaway account into your normal
(non-private) profile, or Firefox may switch your active X account there.

Full browser automation (`tools/create_session_browser.py`, upstream) is the other
option but needs a Chromium-family browser (`nodriver` does not support Firefox);
this box only has Firefox, so the manual cookie path above is the supported fallback.

## 2. Start the stack

```bash
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml logs -f nitter   # watch for auth errors
```

`sessions.jsonl` is mode `0600` (readable only by its owner), so the container must
run as that user. The compose file defaults to uid/gid `1000`; if your `id -u` differs,
start it with `NITTER_UID=$(id -u) NITTER_GID=$(id -g) docker compose up -d` (otherwise
Nitter crash-loops on `cannot open: ./sessions.jsonl`).

A healthy start serves RSS:

```bash
curl -s http://localhost:8080/janleike/rss | head -40   # expect RSS <item> entries
```

"Bad Authentication Data" / empty feeds = bad or expired sessions → regenerate (step 1).

## 3. paper-watch uses it automatically

`config.yaml` lists `http://localhost:8080` first in `nitter_instances`, so the searcher
hits the local instance first and falls back to public instances only if it's down.

## Maintenance

Session tokens expire or get flagged. When the Twitter source goes quiet, regenerate
sessions (step 1) and `docker compose -f docker-compose.yml restart nitter`. Keeping
several accounts in `sessions.jsonl` means one bad token doesn't take the source down.

## Stop

```bash
docker compose -f docker-compose.yml down        # keep redis cache volume
docker compose -f docker-compose.yml down -v     # also wipe the cache
```
