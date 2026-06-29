# ARCHITECTURE.md — cTrader Bots on VPS

**Last updated:** 2026-06-29 15:35 UTC

---

## 1. Naming & Repositories

| Что | Где |
|---|---|
| **VPS host** | `193.233.19.171` (root@193.233.19.171) |
| **GitHub GOLD** | https://github.com/impowery/Goldctraderbot |
| **GitHub BTC** | https://github.com/impowery/BTC-VPS-bot |
| **Dashboard URL** | http://193.233.19.171:8080/report_latest.html |
| **Trade receiver** | http://193.233.19.171:8089/api/trade (POST) |

**Правило:** GOLD бот — наш, BTC бот — НЕ ТРОГАТЬ без разрешения.

---

## 2. VPS File Layout

```
/root/
├── Goldctraderbot/                    ← GOLD cTrader bot (наш)
│   └── gold_ctrader_bot/
│       ├── gold_mcp_bot_remote.py     ← MAIN bot code (1331 строка)
│       ├── strategy.py                ← EMA20 + ADX(14) + ATR(14) + range filter
│       ├── .env                       ← GOLD config (gitignored, секреты)
│       ├── state_remote.json          ← bot state (entries, SL, cooldown, PnL)
│       ├── backfill_trades.py         ← sync deals from cTrader API
│       ├── MY_RESPONSIBILITY.md       ← rules (не менять без разрешения)
│       └── trades_gold_ctrader.jsonl  ← trade log (raw)
│
├── BTC-VPS-bot/                       ← BTC cTrader bot (НЕ ТРОГАТЬ)
│   └── gold_ctrader_bot/
│       ├── gold_mcp_bot_remote.py     ← same code as GOLD but different .env
│       ├── .env                       ← BTC config
│       └── state_btc_remote.json      ← BTC state
│
├── bots/                              ← shared infrastructure
│   ├── gen_report.py                  ← dashboard HTML generator (cron 5 min)
│   ├── ctrader_trades_server.py       ← HTTP :8089 trade receiver
│   ├── ctrader_alerts.py              ← Telegram alerts (cron 15 min)
│   ├── dashboard_server.py            ← HTTP :8080 dashboard server
│   ├── health_check.py                ← 30 min health check
│   ├── backup_dashboard.py            ← daily 21:00 backup to GitHub
│   ├── btc_bot.py                     ← HL paper BTC bot
│   ├── hype_bot.py                    ← HL paper HYPE bot
│   ├── wti_bot.py                     ← HL paper WTI bot
│   ├── xyz100_bot.py                  ← HL paper XYZ100 bot
│   ├── paradex_btc_bot.py             ← Paradex BTC bot
│   ├── trades_gold_ctrader.jsonl      ← GOLD trade log (copy, used by dashboard)
│   ├── trades_btc_ctrader.jsonl       ← BTC trade log
│   ├── report_latest.html             ← dashboard output (cron-regenerated)
│   └── logs/                          ← all logs
│       ├── gold_remote.log            ← GOLD cTrader bot log
│       ├── btc_remote.log             ← BTC cTrader bot log
│       ├── backfill.log
│       └── ctrader_alerts.log
│
├── start_bots.sh                      ← @reboot script (cron)
└── MY_RESPONSIBILITY.md               ← rules copy

/etc/systemd/system/
└── gold-remote.service                ← GOLD bot systemd unit (auto-restart)

/root/.env (нет, только в подкаталогах ботов)
```

---

## 3. Process Architecture

### 3.1 GOLD cTrader Bot (наш)

```
systemd: gold-remote.service
   ↓ (active, enabled, Restart=always)
ExecStart=/usr/bin/python3 -u gold_mcp_bot_remote.py
   ↓
Bot tick loop (every 60 sec):
   1. get_balance → cTrader MCP
   2. fetch_candles M5 (100 candles) → cTrader MCP
   3. calc EMA20 + ADX(14) + ATR(14) + today range
   4. sync_position → check open positions
   5. if has_position: manage_position (trailing SL, BE, TP, scale-in)
   6. if no position: check signal + cooldown → open_entry
   7. save state_remote.json
   8. POST trade events to :8089 (if any)
```

**Запуск/управление:**
```bash
systemctl status gold-remote      # статус
systemctl restart gold-remote     # рестарт
systemctl stop gold-remote        # стоп
journalctl -u gold-remote -f      # live лог
tail -f /root/bots/logs/gold_remote.log
```

### 3.2 BTC cTrader Bot (НЕ ТРОГАТЬ)

```
screen: btc-remote (PID 28987)
   ↓
cd /root/BTC-VPS-bot/gold_ctrader_bot && python3 -u gold_mcp_bot_remote.py
   ↓ (same code, different .env)
```

**Внимание:** BTC бот на screen, не systemd. После ребута VPS поднимается через `start_bots.sh`.

### 3.3 Paper Bots (HL perpetuals)

```
screen: gold      → /root/gold_bot.py        (PAXG)
screen: btc       → /root/btc_bot.py          (BTC)
screen: hype      → /root/hype_bot.py         (HYPE)
screen: wti       → /root/wti_bot.py          (WTI)
screen: xyz100    → /root/xyz100_bot.py       (XYZ100)
screen: paradex-btc → /root/paradex_btc_bot.py
screen: ctrader-trades → /root/bots/ctrader_trades_server.py  (HTTP :8089)
```

---

## 4. Strategy (GOLD cTrader)

**Файл:** `strategy.py`
**Логика:** trend-following with scale-in on pullbacks

### 4.1 Сигнал входа (`should_enter`)
- LONG: `close > EMA20` AND `EMA20 rising` AND `ADX ≥ 25` AND `range_pos < 0.7`
- SHORT: `close < EMA20` AND `EMA20 falling` AND `ADX ≥ 25` AND `range_pos > 0.3`
- `range_pos = (price - today_low) / (today_high - today_low)` — блокировка входа на хаях/лоях дня

### 4.2 Управление позицией

| Параметр | Значение | Где |
|---|---|---|
| **MAX_ENTRIES** | 3 | `.env` |
| **ENTRY_VOLUMES** | 0.3, 0.3, 0.3 lots | `.env` |
| **SL** | 3.0 × ATR | `.env` SL_ATR_MULT |
| **TP1** | 1.5 × ATR (close first position fully) | `.env` |
| **TP2** | 4.0 × ATR (close entries with TP) | `.env` |
| **Last entry** | NO TP, rides trend with trailing SL only | code |
| **Break-even** | trigger at +0.2% PnL | `.env` BE_TRIGGER_PCT |
| **Time exit** | 4h if `|PnL| < 1%` | `.env` TIME_EXIT_HOURS |
| **Scale-in cooldown** | 300s (5 min) | `.env` SCALE_IN_COOLDOWN_SEC |
| **SL cooldown** | 1800s (30 min) → escalation 2x → max 7200s | `.env` COOLDOWN_AFTER_SL |

### 4.3 Scale-in логика (добивка на откатах)
```
if len(entries) < MAX_ENTRIES:
   if time_since_last_scale >= SCALE_IN_COOLDOWN_SEC:
      if pnl_pct > -0.5:                          # не усреднять большой убыток
         if distance_ok = abs(price-avg) >= 0.5*ATR:    # не слишком близко
            if not_overextended = abs(price-ema) < 1.5*ATR:  # не слишком далеко
               if can_scale = pullback к EMA:
                  open_entry()
```

### 4.4 Trailing SL
- LONG: `extreme_price` обновляется вверх, SL = `extreme - 3*ATR` (только вверх)
- SHORT: `extreme_price` обновляется вниз, SL = `extreme + 3*ATR` (только вниз)

### 4.5 Cooldown после SL
```
consecutive_losses += 1
cooldown = min(COOLDOWN_AFTER_SL * 2^(consec-1), 7200)
# 1-й loss: 1800s (30 min)
# 2-й loss: 3600s (60 min)
# 3-й loss: 7200s (120 min, ceiling)
```
Сбрасывается `consecutive_losses = 0` при PnL ≥ 0.

---

## 5. Configuration

### 5.1 GOLD .env (полный, кроме токенов)

```ini
# MCP
MCP_URL=https://mcp.ctrader.com/trading/mcp
MCP_BEARER_TOKEN=<REDACTED>

# Strategy
SYMBOL_NAME=XAUUSD
SYMBOL_ID=41
LOT_SIZE=100
PIP_DIGITS=5
MONEY_DIGITS=2
MIN_INTERVAL_MINUTES=30
MAX_LOSS_PERCENT=2.5

ENTRY_VOLUMES=0.3,0.3,0.3
MAX_ENTRIES=3
SL_ATR_MULT=3.0
TP1_ATR_MULT=1.5
TP2_ATR_MULT=4.0
TRAIL_ACTIVATE_PCT=0.5
TIME_EXIT_HOURS=4
BE_TRIGGER_PCT=0.2
BE_OFFSET_ATR=0.0

TIMEFRAME=M_5
CANDLE_COUNT=100
CHECK_INTERVAL=60

# VPS sync
VPS_SYNC_ENABLED=true
VPS_SYNC_URL=http://127.0.0.1:8089
VPS_AUTH_TOKEN=<REDACTED>
TRADE_LOG_PATH=/root/bots/trades_gold_ctrader.jsonl
STATE_FILE_PATH=state_remote.json
DRY_RUN=false

# Cooldowns
COOLDOWN_AFTER_SL=1800
SCALE_IN_COOLDOWN_SEC=300
```

### 5.2 BTC .env (НЕ ТРОГАТЬ, для справки)

```ini
SYMBOL_NAME=BTCUSD
SYMBOL_ID=22395
LOT_SIZE=1
ENTRY_VOLUMES=0.3
MAX_ENTRIES=1
SL_ATR_MULT=2.0
TP1_ATR_MULT=1.0
TP2_ATR_MULT=3.0
BE_TRIGGER_PCT=0.5
TIMEFRAME=M_15
CANDLE_COUNT=60
ADX_THRESHOLD=22
COOLDOWN_AFTER_SL=1800
```

---

## 6. Network & Ports

| Порт | Что | Process |
|---|---|---|
| **80** | nginx (reverse proxy) | nginx |
| **8080** | Dashboard HTML server | `python3 -m http.server 8080` |
| **8089** | Trade receiver API | `ctrader_trades_server.py` |
| **22** | SSH | sshd |

**Firewall:** UFW включен, открыты 22, 80, 8080, 8089.

---

## 7. Cron Jobs

```cron
@reboot                    /root/start_bots.sh                              # поднимает все screen-сессии
*/5 * * * *                cd /root/bots && python3 gen_report.py           # регенерация дашборда
*/10 * * * *               cd /root/Goldctraderbot/gold_ctrader_bot && python3 backfill_trades.py --days 7   # синк сделок GOLD
*/15 * * * *               /usr/bin/python3 /root/bots/ctrader_alerts.py    # Telegram уведомления
0 21 * * *                 python3 /root/bots/backup_dashboard.py           # бэкап на GitHub
*/30 * * * *               python3 /root/bots/health_check.py               # health check paper bots
```

---

## 8. Data Flow

```
cTrader MCP (https://mcp.ctrader.com/trading/mcp)
    ↑↓ Bearer token
    │
gold-remote.service (GOLD bot)
    │
    ├──→ state_remote.json (local state)
    ├──→ trades_gold_ctrader.jsonl (raw trade log)
    │
    └──→ POST :8089/api/trade (на каждое событие)
              │
              ↓
         ctrader_trades_server.py
              │
              ├──→ /root/bots/trades_gold_ctrader.jsonl
              │
              ↓ (cron */5 min)
         gen_report.py
              │
              ├──→ reads all trades_*.jsonl
              ├──→ reads state_*.json
              │
              └──→ /root/bots/report_latest.html
                      │
                      ↓ (HTTP :8080)
                   Dashboard в браузере

         ctrader_alerts.py (cron */15 min)
              │
              └──→ Telegram bot @ 354703083
```

---

## 9. Live Status (snapshot 2026-06-29 15:35 UTC)

### 9.1 GOLD bot
- **Service:** active (running), PID 46858
- **Balance:** $101,059.20 (старт $98,885.88, **+$2,173.32**)
- **Open positions:** 2 SHORT (0.6 lots)
  - pid=269391913 entry=$4022.29 SL=$4046.83 TP=$3989.58
  - pid=269405067 entry=$4029.03 SL=$4052.32 TP=$3997.98
- **Current:** $4021.72 | EMA=4024.8 | ADX=33.0 | ATR=5.57
- **today range:** $3999.95 – $4068.51

### 9.2 GitHub sync
- **GOLD repo:** commit `14d037c` (HEAD of main) — все правки залиты
- `gold_mcp_bot_remote.py` md5 = `edb04f3c` (совпадает VPS ↔ GitHub)
- `strategy.py` md5 = `832b3b89` (совпадает VPS ↔ GitHub)
- `.env` НЕ в git (содержит токены)

---

## 10. Recovery Procedures

### 10.1 Если VPS перезагрузился
```bash
# GOLD — должен подняться автоматически через systemd
systemctl status gold-remote

# Если нет — ручной старт
systemctl start gold-remote
```

### 10.2 Если бот упал и не поднимается
```bash
# 1. Лог
tail -100 /root/bots/logs/gold_remote.log
journalctl -u gold-remote -n 100 --no-pager

# 2. Проверить state
cat /root/Goldctraderbot/gold_ctrader_bot/state_remote.json | python3 -m json.tool

# 3. Проверить что MCP доступен
curl -I https://mcp.ctrader.com/trading/mcp

# 4. Перезапуск
systemctl restart gold-remote
```

### 10.3 Если .env потерян (восстановление с нуля)
- cTrader токены: взять из cTrader Web → OpenAPI → Generate token (нужен $100K demo cTID для GOLD)
- VPS_AUTH_TOKEN: `gold2026secret`
- Telegram bot token: `8664275234:AAHUHIdruK4FWjIioRwtqU1PGQeceyeBk-g`
- Telegram chat ID: `354703083`

### 10.4 Если нужно откатить код
```bash
cd /root/Goldctraderbot  # (нет, напрямую на VPS нет git)
# Восстановить из GitHub:
scp -r gold_ctrader_bot/gold_mcp_bot_remote.py root@193.233.19.171:/root/Goldctraderbot/gold_ctrader_bot/
systemctl restart gold-remote
```

---

## 11. Key Rules (из MY_RESPONSIBILITY.md)

1. **БЕЗ РАЗРЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ НЕ МЕНЯТЬ НИ ОДНОГО ИЗМЕНЕНИЯ** в коде/.env/конфигах
2. **GOLD бот — наш. BTC бот — НЕ ТРОГАТЬ.**
3. **После ЛЮБЫХ правок — перезапустить сервис и проверить в логе**
4. **Никогда не говорить "всё в порядке" без проверки реальных сделок**
5. **Разрешено без подтверждения:** чтение логов/state, аварийный рестарт после падения

---

## 12. Key Lessons (из истории багов)

- **Bug #20:** После правки кода не перезапустили screen → бот работал со старым кодом → **-$1,198 loss**. С тех пор: ВСЕГДА restart после правок.
- **Bug #21:** `close_all()` сбрасывал `sl_cooldown_until = 0` → бот сразу лез в новую сделку после SL. Пофикшено.
- **VPS reboot 2026-06-29:** 3 SHORT позиции без управления ~50 мин → повезло, TP сработал у брокера → +$1,645. С тех пор: systemd с `Restart=always`.
- **Scale-in bug:** `last_scale_in_time=0` после `close_all` обходил cooldown. Пофикшено 2026-06-29 (commit `14d037c`).

---

**Документ поддерживается в актуальном состоянии.** Любые архитектурные изменения → обновлять этот файл + коммит в GitHub.
