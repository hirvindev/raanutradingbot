# Deploying

## Server, dashboard, API — already automatic

`git push` to `main` → Railway builds and deploys. Nothing else to do.

That covers `server.py`, every strategy and engine module, `RaanuTradingBot.html`,
`sw.js` and the manifest. If you changed only those, pushing **is** the deploy.

Check it landed:

```bash
curl -s https://raanu.up.railway.app/api/health | python3 -m json.tool
```

Give it 2–4 minutes. `state.persistent` must be `true` and `state.data_dir`
must read `/data` — if it says `/tmp`, the volume did not mount and the trade
log will be wiped on the next redeploy.

## Android app — one command

```bash
./deploy-mobile.sh
```

Bumps the version code, builds the bundle, verifies it, uploads it to Play
internal testing. `--build` stops after the build if you only want the file.

It exists because every step of doing this by hand went wrong at least once:

- a release signed with the **debug key** (Play rejects it, with an error that
  names the symptom and not the cause)
- a version code that **reverted on `expo prebuild`**, because it lived only in
  `build.gradle` and not in `app.json`
- **three bundles stacked in one release**, which Play errors on as "completely
  shadowed" and refuses to roll out

The script checks all three: it writes the version code to both files, asserts
the built bundle carries the release certificate, and sets the track's version
list explicitly so a release can only ever contain one.

It targets the **internal** track and nothing else. Production has a
12-tester/14-day gate and unresolved regulatory questions about a public
stock-signal app; that must never be reachable by a script.

### One-time setup for the upload step

Until this is done, `./deploy-mobile.sh` builds and verifies, then tells you the
bundle is on your Desktop to upload by hand. Only the account owner can do this.

1. **Play Console → Setup → API access** → link a Google Cloud project
2. **Create a service account** (it hands you off to Google Cloud) →
   **Keys → Add key → JSON** → download
3. Back in **Play Console → Users and permissions**, find the service account
   and grant it, for `app.raanu.mobile` only:
   - *Release to testing tracks*
   - *View app information*
   Do **not** grant production release rights. The script cannot use them, and
   an account that cannot do a thing cannot do it by accident.
4. Move the key somewhere sane:

```bash
mkdir -p ~/.secrets && chmod 700 ~/.secrets
mv ~/Downloads/<the-key>.json ~/.secrets/play-service-account.json
chmod 600 ~/.secrets/play-service-account.json
```

Override the path with `PLAY_SERVICE_ACCOUNT_JSON` if you keep it elsewhere.

Permissions can take a few minutes to propagate; a 401 on the first run usually
means "too soon", not "wrong key".

## What is deliberately NOT automated

**iOS.** APNs auth keys are issued only to paid Apple Developer Program members
($99/year, recurring), so there is nothing to automate until that is a decision
that has been made. See the iPhone section in `CLAUDE.md`.

**Production releases on Play.** See above.

**`ALPACA_MODE`.** Stays `paper`. No script touches it.
