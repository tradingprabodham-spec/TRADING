# delta_ema_bot — 24/7 EMA Crossover Auto-Trader (BTCUSD, Delta Exchange)

Runs your exact strategy continuously on a server — no browser tab, no laptop needed.

**Strategy:** 9/200 EMA crossover → 1 contract. 21/200 EMA crossover → additional 1 contract.
Runs on **BTCUSD and XAUTUSD simultaneously**, independently. Target/stoploss (1:1 risk-reward):
- **BTCUSD** → plain $1000 price move
- **XAUTUSD** → ₹1000, converted to the equivalent $ price move (gold moves far less than $1000)

---

## 1. Setup (5 minutes)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp config.example.env .env
```

Edit `.env`:
- Get your API key/secret from Delta Exchange → Account → API Keys (create a **separate testnet key** first, from the testnet site).
- Leave `DELTA_ENV=testnet` and `DRY_RUN=true` until you're confident.

Load the `.env` file before running:

```bash
export $(grep -v '^#' .env | xargs)   # macOS/Linux quick way to load .env into the shell
python3 delta_ema_bot.py
```

Watch `bot.log` — you should see it fetch candles, log EMA checks, and (in DRY_RUN) log simulated trades without placing real orders.

## 2. Test on Testnet for real (still no real money)

Once DRY_RUN logs look correct, set `DRY_RUN=false` while keeping `DELTA_ENV=testnet`. This places real orders — but on Delta's testnet, so it's fake money.

## 3. Go live

Only after you're satisfied: generate a **live** API key from Delta Exchange's real account, update `.env` with `DELTA_ENV=live` and the live key/secret, keep `DRY_RUN=false`. Real orders, real money, from here on.

## 4. Deploy so it runs 24/7 (pick one)

**Option A — Small VPS (most control, ~$4-6/month)**
Any provider (DigitalOcean, Hetzner, AWS Lightsail, etc.). SSH in, repeat step 1, then keep it running permanently with `systemd` or `tmux`/`screen`.

**Option B — Railway / Render (easiest, free tier available)**
Connect this GitHub repo as a "Worker"/"Background Service" (not a web service) on Railway or Render, set the environment variables in their dashboard, and deploy. It restarts automatically on crash and stays up 24/7.

**Option C — Raspberry Pi at home**
Same as Option A but on a Pi plugged into your router.

## 5. Notifications

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` to get a Telegram message every time a trade opens or closes. (Message @BotFather to create a bot, and @userinfobot to get your chat id.)

## 6. State & recovery

Open positions are saved to `bot_state.json` after every change. If the process restarts, it picks up exactly where it left off.

## Safety notes

- **This places real orders automatically with no per-trade confirmation.** Don't run it live unless you've watched DRY_RUN and testnet behave exactly as expected first.
- Keep `.env` out of git. Never paste your live secret into a chat, a repo, or share it with anyone.
- If you stop trusting the bot's behavior, kill the process — any open positions on Delta will remain open until you close them manually.
- This is a tool that executes the rules you specified — it is not financial advice.
