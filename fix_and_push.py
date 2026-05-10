import os, subprocess

FOLDER = r"C:\Users\Archana Arjunraj\OneDrive\Desktop\Algo Trading"
FILE = os.path.join(FOLDER, "RaanuTradingBot.html")

print("Reading file...")
with open(FILE, "r", encoding="utf-8") as f:
    c = f.read()

print(f"File size: {len(c)} chars")

# Fix all branding
replacements = [
    ("AlgoTrader v2.1 \u2014 Trade 212", "RaanuTradingBot \u2014 Trade 212"),
    ("AlgoTrader v2.1 - Trade 212",  "RaanuTradingBot - Trade 212"),
    ("Algo<em>Trader</em>",          "Raanu<em>TradingBot</em>"),
    ("AlgoTrader v2.1 starting",     "RaanuTradingBot v2.1 starting"),
    ("AlgoTrader",                   "RaanuTradingBot"),
]

for old, new in replacements:
    count = c.count(old)
    if count:
        c = c.replace(old, new)
        print(f"  Replaced '{old}' x{count}")

with open(FILE, "w", encoding="utf-8") as f:
    f.write(c)
print("File saved!")

# Git push
os.chdir(FOLDER)
subprocess.run(["git", "add", "RaanuTradingBot.html"])
subprocess.run(["git", "commit", "-m", "Fix: RaanuTradingBot branding + 370 stock scanner"])
subprocess.run(["git", "push", "--force"])
print("\nDone! Vercel will redeploy in 30 seconds.")
print("Refresh: https://raanutradingbot.vercel.app")
