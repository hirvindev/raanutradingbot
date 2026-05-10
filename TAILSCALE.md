# Tailscale Setup — Access RaanuTradingBot from Any of Your Devices

Tailscale gives every device of yours (laptop, phone, tablet, work computer)
a private IP that only your devices can see. The bot stays on your home laptop;
you reach it from anywhere as if you were on the same network.

No public URL. No port forwarding. No hackers finding it on the open internet.

## One-time setup (10 minutes)

### Step 1 — Make a free Tailscale account
Go to https://tailscale.com → "Get started" → sign in with Google or Microsoft.

The free tier covers up to 100 devices. Plenty for personal use.

### Step 2 — Install Tailscale on the LAPTOP that runs the bot
Download from https://tailscale.com/download/windows.

After install, click the Tailscale icon in your system tray → "Sign in" → use
the same account you just made.

You'll see your laptop appear in the Tailscale admin panel with an IP like
`100.64.x.x`. That's your tailnet IP — write it down.

### Step 3 — Install Tailscale on your phone / other devices
- iPhone: App Store → Tailscale
- Android: Play Store → Tailscale
- Other laptop: same download link as above
- Sign in with the same account

All your devices will now see each other in the Tailscale admin panel.

### Step 4 — Open Windows Firewall for port 8000 (one-off)
The bot listens on port 8000. Windows Firewall blocks incoming connections
from non-localhost by default, including Tailscale. Open it:

1. Press Win, type "Windows Defender Firewall with Advanced Security", Enter
2. Click "Inbound Rules" on the left → "New Rule…" on the right
3. Rule type: **Port** → Next
4. TCP, Specific local ports: **8000** → Next
5. Allow the connection → Next
6. Apply to all profiles → Next
7. Name: **RaanuTradingBot** → Finish

### Step 5 — Run the bot
Double-click `start.bat` on the laptop. The console will show:

```
Dashboard:   http://localhost:8000
Tailscale:   http://<your-tailscale-ip>:8000
```

### Step 6 — Open from any of your devices
On your phone, open Safari/Chrome and type:

```
http://100.64.x.x:8000
```

(use the actual Tailscale IP of your laptop, not the literal `x.x`)

That's it. The dashboard works from anywhere as long as both devices have
Tailscale running.

## Tips

- Bookmark the URL on your phone for one-tap access.
- Tailscale's "MagicDNS" feature lets you use `http://laptopname:8000`
  instead of the IP. Enable it in the Tailscale admin panel → DNS.
- The laptop has to be ON and `start.bat` running for the bot to be reachable.
  If you want it always-on, leave the laptop awake or look at running it on
  a small home server / Raspberry Pi.
- If the bot connection drops, check that Tailscale is still signed in on
  both devices (system tray on Windows, app on phone).

## Security model

- Anyone on your tailnet can reach the bot. So don't add untrusted people to
  your tailnet.
- The bot has zero authentication of its own — it trusts whoever can reach it.
  Tailscale IS the auth layer. Treat your Tailscale account password and 2FA
  as seriously as your bank password.
- The T212 API key never leaves the laptop. Even Tailscale doesn't see it.
