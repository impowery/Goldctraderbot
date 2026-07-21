# BOT_CONTEXT.md — Единый источник правды

> **Этот файл — единственный контекстный документ по cTrader ботам.**
> Обновлять при изменениях. Дубликаты (`ARCHITECTURE.md`) удалены.
> Правила — в отдельном файле `MY_RESPONSIBILITY.md`.
>
> **Last updated:** 2026-06-30

---

## 1. Что за продукт

**2 live бота на cTrader Remote MCP** + 5 paper ботов на Hyperliquid.
Live боты работают 24/7 на Linux VPS (193.233.19.171) через cTrader Remote MCP — без cTrader Desktop, без Windows.

**Dashboard:** http://193.233.19.171:8080/report_latest.html (7 ботов, обновление 5 мин)

---

## 2. Live боты (cTrader Remote MCP)

| Бот | Symbol | Account | Balance | Volume | Leverage | Process |
|---|---|---|---|---|---|---|
| **GOLD-CTRADER** | XAUUSD | cTrader demo $100K (#2) | $102,608.45 | 0.3 lot × 3 | 1:10 | systemd `gold-remote.service` |
| **BTC-CTRADER** | BTCUSD | cTrader demo $5K (#1) | $4,838.65 | 0.3 lot × 1 | 1:10 | screen `btc-remote` |

**PnL на 2026-06-30:**
- GOLD: **+$3,061.16** (40 сделок за 7 дней, win rate 62.5%)
- BTC: -$161.35 (55 сделок, win rate 31% — стратегия не отлажена)

**Telegram alerts:** @AITradingAlertPNLBot (chat_id 354703083)
- Trade alerts при каждой закрытой сделке (HTML + monospace blocks)
- Daily summary в 23:50 MSK
- Anomaly alerts (3 consec losses, large loss, no trades 4h+)

---

## 3. VPS доступ и структура файлов

### SSH доступ
```
ssh root@193.233.19.171
password: <REDACTED — взять из Notes/Keepass>
```
Локально (Z environment): `/home/z/.venv/bin/python3 /home/z/my-project/scripts/ssh_helper.py exec "command"`

### Структура на VPS
```
/root/
├── Goldctraderbot/gold_ctrader_bot/     ← GOLD bot (НАШ)
│   ├── gold_mcp_bot_remote.py           ← главный бот (1331 строка)
│   ├── strategy.py                      ← EMA20 + ADX(14) + ATR(14) + range filter
│   ├── .env                             ← GOLD config (gitignored)
│   ├── state_remote.json                ← runtime state
│   ├── backfill_trades.py               ← sync deals from cTrader API
│   ├── MY_RESPONSIBILITY.md             ← правила (не менять без разрешения)
│   ├── BOT_CONTEXT.md                   ← ЭТОТ файл (копия)
│   └── trades_gold_ctrader.jsonl        ← trade log (raw, для backfill)
│
├── BTC-VPS-bot/gold_ctrader_bot/        ← BTC bot (НЕ ТРОГАТЬ)
│   ├── gold_mcp_bot_remote.py           ← same code, different .env
│   ├── .env                             ← BTC config
│   └── state_btc_remote.json
│
├── bots/                                ← shared infrastructure
│   ├── gen_report.py                    ← дашборд HTML (cron */5)
│   ├── ctrader_trades_server.py         ← HTTP :8089 trade receiver
│   ├── ctrader_alerts.py                ← Telegram alerts (cron */15)
│   ├── dashboard_server.py              ← HTTP :8080 dashboard server
│   ├── health_check.py                  ← health check paper bots (cron */30)
│   ├── backup_dashboard.py              ← daily GitHub backup (cron 21:00)
│   ├── btc_bot.py, hype_bot.py, wti_bot.py, xyz100_bot.py  ← HL paper
│   ├── paradex_btc_bot.py
│   ├── trades_gold_ctrader.jsonl        ← GOLD trade log (используется дашбордом)
│   ├── trades_btc_ctrader.jsonl         ← BTC trade log
│   ├── report_latest.html               ← дашборд output
│   └── logs/
│       ├── gold_remote.log              ← GOLD bot log
│       ├── btc_remote.log               ← BTC bot log
│       ├── backfill.log
│       └── ctrader_alerts.log
│
├── start_bots.sh                        ← @reboot script
├── MY_RESPONSIBILITY.md                 ← правила (копия)
├── BOT_CONTEXT.md                       ← ЭТОТ файл (копия в root)
└── SERVER_CONTEXT.md                    ← общий контекст всех VPS проектов
                                          (боты + контент-фабрика + n8n и т.д.)

/etc/systemd/system/
└── gold-remote.service                  ← GOLD bot systemd (auto-restart, enabled)
```

### Команды управления GOLD ботом
```bash
systemctl status gold-remote              # статус
systemctl restart gold-remote             # рестарт
systemctl stop gold-remote                # стоп
journalctl -u gold-remote -f              # live лог
tail -f /root/bots/logs/gold_remote.log   # лог из файла
```

---

## 4. GitHub репозитории

| Repo | URL | Что |
|---|---|---|
| Goldctraderbot | https://github.com/impowery/Goldctraderbot | GOLD bot code + BOT_CONTEXT.md + MY_RESPONSIBILITY.md |
| BTC-VPS-bot | https://github.com/impowery/BTC-VPS-bot | BTC bot (НЕ ТРОГАТЬ) |

**Токен GitHub:** `<REDACTED — взять из Notes/Keepass>`

**Важные файлы на GitHub (не секретные):**
- `gold_ctrader_bot/gold_mcp_bot_remote.py` — основной код бота
- `gold_ctrader_bot/strategy.py` — стратегия
- `gold_ctrader_bot/README.md` — старый README (про Windows MCP, устарел)
- `BOT_CONTEXT.md` — этот файл (в корне репо)
- `MY_RESPONSIBILITY.md` — правила
- `ctrader_alerts.py` — Telegram alerts (в корне репо)

**НЕ в git (секреты):**
- `.env` (содержит MCP_BEARER_TOKEN, VPS_AUTH_TOKEN)
- `state_remote.json` (runtime state)

---

## 5. Стратегия (GOLD cTrader)

**Файл:** `strategy.py`
**Логика:** trend-following with scale-in on pullbacks

### 5.1 Сигнал входа (`should_enter`)
- LONG: `close > EMA20` AND `EMA20 rising` AND `ADX ≥ 25` AND `range_pos < 0.7`
- SHORT: `close < EMA20` AND `EMA20 falling` AND `ADX ≥ 25` AND `range_pos > 0.3`
- `range_pos = (price - today_low) / (today_high - today_low)` — блокировка входа на хаях/лоях дня

### 5.2 Параметры (из .env)

| Параметр | GOLD | BTC | Описание |
|---|---|---|---|
| TIMEFRAME | M_15 | M_15 | таймфрейм свечей (был M_5, перешёл на M_15) |
| CANDLE_COUNT | 100 | 60 | сколько свечей тянуть |
| EMA_PERIOD | 14 | — | период EMA (был 20, стал 14 — быстрее) |
| ENTRY_VOLUMES | 0.7,0.5 | 0.3 | объёмы входа (lots), 2 входа одновременно |
| MAX_ENTRIES | 2 | 1 | max entries (было 3 scale-in, стало 2 одновременно) |
| SL_ATR_MULT | 3.0 | 2.0 | SL = N × ATR |
| TP1_ATR_MULT | 3.0 | 1.0 | TP1 (close first position fully), был 1.5 |
| TP2_ATR_MULT | 4.5 | 3.0 | TP2 (close entries with TP), был 4.0 |
| BE_TRIGGER_PCT | 0.5 | 0.5 | break-even trigger (% PnL), был 0.2 |
| TRAIL_ACTIVATE_PCT | 0.4 | — | PnL% при котором начинает работать trailing SL |
| TIME_EXIT_HOURS | 4 | 4 | exit if \|PnL\| < 1% |
| SCALE_IN_COOLDOWN_SEC | 300 | — | между входами |
| SCALE_IN_DISTANCE_MULT | 1.0 | — | min откат от avg для scale-in (×ATR) |
| PULLBACK_MAX_MULT | 3.0 | — | max дистанция цены от EMA для входа (×ATR), был 1.0 |
| CONSEC_LOSS_COUNT | 2 | — | сколько лосей подряд → пауза |
| CONSEC_LOSS_PAUSE_SEC | 1800 | — | пауза после N лосей (сек) |
| TREND_FILTER_ENABLED | true | — | M30 trend filter вкл/выкл |
| TREND_FILTER_TF | M_30 | — | таймфрейм для trend filter |
| M30 EMA period | 41 | — | EMA41 на M30 (20.5 часов ≈ 1 день), было 20 |
| Daily trend filter | H1 open | — | price > today's H1 open → только LONG |
| COOLDOWN_AFTER_SL | 1800 | 1800 | базовый cooldown после SL (эскалация 30→60, max 60 мин) |
| ADX_THRESHOLD | 20 | 22 | минимальный ADX для входа (был 25), configurable в .env |
| Daily range filter | 0.95 | — | LONG skip если >95% дня (был 0.7), SHORT skip если <5% (был 0.3) |

### 5.3 Scale-in логика (добивка на откатах)
```
if len(entries) < MAX_ENTRIES:
   if time_since_last_scale >= SCALE_IN_COOLDOWN_SEC:
      if pnl_pct > -0.5:                          # не усреднять большой убыток
         if distance_ok = abs(price-avg) >= SCALE_IN_DISTANCE_MULT*ATR:  # реальный откат
            if not_overextended = abs(price-ema) < 1.5*ATR:  # не слишком далеко
               if can_scale = pullback к EMA:
                  open_entry()
```

### 5.4 Trailing SL
- LONG: `extreme_price` обновляется вверх, SL = `extreme - 3*ATR` (только вверх)
- SHORT: `extreme_price` обновляется вниз, SL = `extreme + 3*ATR` (только вниз)

### 5.5 Last entry rides trend
3-я (последняя) позиция не имеет TP — едет по тренду с trailing SL только.

### 5.6 Cooldown после SL
```
consecutive_losses += 1
cooldown = min(COOLDOWN_AFTER_SL * 2^(consec-1), 7200)
# 1-й loss: 1800s (30 min)
# 2-й loss: 3600s (60 min)
# 3-й loss: 7200s (120 min, ceiling)
```
Сбрасывается `consecutive_losses = 0` при PnL ≥ 0.

### 5.7 Market hours
- **BTC:** 24/7
- **GOLD:** Mon-Fri 01:15-23:45 MSK

---

## 6. Volume sizing — ФОРМУЛА ПЕРЕСЧЁТА

### Универсальная формула для любого счёта
```
new_volume = current_volume × (new_balance / old_balance) × (old_leverage / new_leverage)
```

### Текущие настройки

| Бот | Счёт | Leverage | Entry | Max (3x) | Margin/entry | SL risk | TP2 profit | Risk % |
|---|---|---|---|---|---|---|---|---|
| GOLD | $100K demo | 1:10 | 0.3 lot | 0.9 lot | $12,030 (12%) | $252 | $504 | 0.25% |
| BTC | $5K demo | 1:10 | 0.3 lot | 0.3 lot | $599 (12%) | $48 | $96 | 1.0% |

### Volume conversion (cTrader API)
```
volume_cents = lots × LOT_SIZE × 100

XAUUSD (LOT_SIZE=100):
  0.3 lots → 0.3 × 100 × 100 = 3000 cents
  0.01 lots → 0.01 × 100 × 100 = 100 cents (min)

BTCUSD (LOT_SIZE=1):
  0.3 lots → 0.3 × 1 × 100 = 30 cents
  0.01 lots → 0.01 × 1 × 100 = 1 cent (min)
```

### Pipettes conversion (SL/TP)
```
pipettes = price_difference × 10^PIP_DIGITS

XAUUSD (PIP_DIGITS=5):
  $4,003.93 → 400,393,000 pipettes
  SL $10 below → 10 × 100,000 = 1,000,000 pipettes

relativeStopLoss/TakeProfit — в PIPETTES, округлённых до 1000 (XAU) / 100 (BTC).
```

---

## 7. cTrader Remote MCP — API

### URL и auth
```
URL: https://mcp.ctrader.com/trading/mcp
Auth: Authorization: Bearer <token>
Token format: base64({"plant":"ctrader","environment":"demo","token":"<hash>"})
```

### Tokens
- **GOLD ($100K demo):** `<REDACTED — взять из cTrader Web → OpenAPI>`
- **BTC ($5K demo):** `<REDACTED — взять из cTrader Web → OpenAPI>`

### 16 tools available
- `get_balance`, `get_symbols`, `get_assets`, `get_spot_prices`, `get_trendbars`
- `get_positions`, `get_position_details`, `get_pending_orders`
- `get_order_history`, `get_deals`, `get_version`
- `create_order`, `amend_position`, `close_position`, `amend_order`, `cancel_order`

### Response format
SSE (Server-Sent Events): `event: message\ndata: {"result":{...}}`

### Limitations
- Один токен на cTID (нужен отдельный cTID для каждого бота)
- `get_trendbars` требует fromTimestamp + toTimestamp
- `relativeStopLoss/TakeProfit` в pipettes, округлённые до 1000 (XAU) / 100 (BTC)
- Min volume зависит от брокера: 0.01 lots (cTrader demo) vs 1.0 lot (PipFarm demo)
- `amend_position` SL/TP precision: 2 digits for XAUUSD, 3 for BTCUSD

---

## 8. Network & Ports

| Порт | Что | Process |
|---|---|---|
| 80 | nginx (reverse proxy) | nginx |
| 8080 | Dashboard HTML server | `python3 -m http.server 8080` |
| 8089 | Trade receiver API | `ctrader_trades_server.py` |
| 22 | SSH | sshd |

**Firewall:** UFW включен, открыты 22, 80, 8080, 8089.

---

## 9. Cron jobs (VPS)

```cron
@reboot                    /root/start_bots.sh                              # paper bots
*/5 * * * *                cd /root/bots && python3 gen_report.py           # дашборд
*/10 * * * *               cd /root/Goldctraderbot/gold_ctrader_bot && python3 backfill_trades.py --days 7
*/15 * * * *               TG_CHAT_ID=354703083 ... python3 /root/bots/ctrader_alerts.py
55 20 * * *                TG_CHAT_ID=354703083 ... python3 /root/bots/ctrader_alerts.py  # backup daily
0 21 * * *                 GITHUB_TOKEN=... python3 /root/bots/backup_dashboard.py
*/30 * * * *               python3 /root/bots/health_check.py
```

---

## 10. Data Flow

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
              └──→ /root/bots/report_latest.html → :8080

         ctrader_alerts.py (cron */15 min)
              │
              └──→ Telegram @AITradingAlertPNLBot

         backfill_trades.py (cron */10 min)
              │
              └──→ перезаписывает trades_gold_ctrader.jsonl свежими сделками из cTrader API
```

---

## 11. Live метрики (snapshot 2026-07-01 15:57 UTC)

### GOLD bot
- **Service:** active (running), systemd, auto-restart включён
- **Balance:** $101,967.99 (старт $98,885.88, **+$3,082.11**)
- **Сделок за 7 дней:** 53 (31W / 22L, win rate 58.5%)
- **Total PnL:** +$2,596.31

### Daily breakdown
| День | Сделок | W/L | Win% | PnL |
|---|---|---|---|---|
| 2026-06-26 | 22 | 11W/11L | 50% | -$870.48 |
| 2026-06-29 | 13 | 11W/2L | 85% | +$2,534.10 |
| 2026-06-30 | 13 | 8W/5L | 62% | +$1,397.54 |
| 2026-07-01 | 5 | 1W/4L | 20% | -$488 (4 LONG убытка утром) |

### По причинам закрытия
| Reason | Сделок | Win% | Total PnL |
|---|---|---|---|
| TP_OR_SL | 28 | 57% | +$1,553.79 |
| TIME (4h exit) | 10 | 70% | +$1,504.14 |
| OPEN/SCALP | 2 | 100% | +$3.23 |

---

## 12. Recovery Procedures

### Если VPS перезагрузился
```bash
# GOLD — должен подняться автоматически через systemd
systemctl status gold-remote

# Если нет — ручной старт
systemctl start gold-remote
```

### Если бот упал и не поднимается
```bash
tail -100 /root/bots/logs/gold_remote.log
journalctl -u gold-remote -n 100 --no-pager
cat /root/Goldctraderbot/gold_ctrader_bot/state_remote.json | python3 -m json.tool
curl -I https://mcp.ctrader.com/trading/mcp
systemctl restart gold-remote
```

### Если .env потерян
- cTrader токены: взять из cTrader Web → OpenAPI → Generate token (нужен $100K demo cTID для GOLD, $5K demo cTID для BTC)
- VPS_AUTH_TOKEN: `<REDACTED — взять из Notes>`
- Telegram bot token: `<REDACTED — взять из @BotFather>`
- Telegram chat ID: `354703083`

### Если нужно откатить код
```bash
# Восстановить из GitHub:
scp gold_ctrader_bot/gold_mcp_bot_remote.py root@193.233.19.171:/root/Goldctraderbot/gold_ctrader_bot/
systemctl restart gold-remote
```

---

## 13. История багов и фиксов

| # | Баг | Фикс |
|---|---|---|
| 1 | JS toggle `{{ }}` в non-f-string | Заменил `{{` → `{` |
| 2 | Balance не обновлялся в дашборде | Добавил `current_balance` |
| 3 | SL formula `price * 0.025` давала $100 вместо $19 | Убрал, ATR-only |
| 4 | Trailing SL dead comparison | `self.current_sl` tracker |
| 5 | Scale-in без cooldown (3 entries за 2 мин) | `SCALE_IN_COOLDOWN_SEC = 300` |
| 6 | External close не записывался | `EXTERNAL_CLOSE` trade record |
| 7 | Windows charmap `→` crash | Убрал unicode, UTF-8 reconfigure |
| 8 | Remote MCP token без `token` field | Полный токен из cTrader Web |
| 9 | relativeStopLoss "invalid precision" | Округление до 100 |
| 10 | Min volume 1.0 lot на PipFarm demo | $100K cTrader demo (min 0.01) |
| 11 | Один токен на все demo аккаунты | Второй cTID с другим email |
| 12 | relativeTakeProfit в points вместо pipettes | `× 10^PIP_DIGITS` |
| 13 | amend_position precision | 2 digits XAU, 3 BTC |
| 14 | TP1 partial close в Hedged mode | Close first position fully |
| 15 | restore_positions конвертировал display prices как pipettes | `float()` напрямую |
| 16 | amend без TP удалял cTrader TP | Always pass TP in amend |
| 17 | Dashboard wrong PnL для BTC | lot_size по symbol_name |
| 18 | amend пересчитывал TP wrong | Stored `tp_price` from entry dict |
| 19 | amend ставил TP на last entry | `is_last_entry` guard |
| 20 | После правки кода не перезапустили screen → -$1,198 loss | ВСЕГДА restart после правок |
| 21 | `close_all()` сбрасывал `sl_cooldown_until=0` | PnL from prices + cooldown escalation |
| 22 | `extreme_price` + `current_sl` reset на scale-in | Only reset on first entry |
| 23 | TP на last entry via amend | `is_last_entry` guard |
| 24 | VPS reboot убил screen с GOLD ботом → 3 SHORT без управления 50 мин | systemd `gold-remote.service` |
| 25 | `last_scale_in_time=0` обходил cooldown | Update на каждый entry + fallback to entry_time |
| 26 | `SCALE_IN_COOLDOWN_SEC` был hardcoded | Вынесен в `.env` |
| 27 | `cp` в cron затирал свежие trades устаревшим файлом | Убран `&& cp` |
| 28 | Last entry получал TP через amend (regression бага #19/#23) — `needs_amend=True` стоял после elif на неправильном отступе, срабатывал всегда; 3-я позиция закрывалась по TP вместо trailing SL | Убран лишний `needs_amend=True`, в amend передаётся `take_profit=None` для last entry |
| 29 | `amend_position`/`close_position` на закрытые позиции генерировали 404 каждую минуту (83 ошибки) | `_remove_stale_position()` — убирает position_id из state когда cTrader возвращает 404 |
| 30 | Scale-in срабатывал на шумовом колебании (0.5×ATR ≈ $2.5) — 3 входа за 17 мин на движении $9, avg price слишком близко к 1-й цене | `SCALE_IN_DISTANCE_MULT=1.0` в `.env` (была 0.5) — нужен реальный откат ≥ 1×ATR |
| 31 | **Trailing SL и BE применялись ко всем позициям одновременно (одна цена SL для всех)** — 3 LONG со SL в одной точке $4022, маркетмейкеры зацепили кластер на лою отката $4021, -$1,527 убытка | Per-entry trailing SL + BE: каждая позиция имеет свой `extreme_price` и `sl_price`, SL считается от entry_price каждой позиции. Сегодня SL были бы $4018/$4010/$4008 — лой $4021 не задел бы ни один |
| 32 | BE_TRIGGER_PCT=0.2% слишком tight — $8 движения на $4000 триггерили BE, позиции закрывались на шуме | `BE_TRIGGER_PCT=0.5%` в `.env` — нужно $20 движения |
| 33 | Нет паузы после серии убытков — сегодня 4 LONG подряд закрылись по SL (-$547 за час) | `CONSEC_LOSS_COUNT=2` + `CONSEC_LOSS_PAUSE_SEC=1800` — 2 лося → 30 мин пауза |
| 34 | Momentum-вход "цена > EMA = LONG" не различает тренд и флэт — в падающем рынке EMA тоже падает, "выше EMA" = ловушка | `PULLBACK_MAX_MULT` — не покупать если цена дальше N×ATR от EMA |
| 35 | Нет проверки старшего таймфрейма — бот открывал LONG на M5 когда M30 EMA падала (контр-тренд) | `TREND_FILTER_ENABLED=true` + `TREND_FILTER_TF=M_30` — если M30 EMA падает, LONG не открывается |
| 36 | `large_loss` anomaly отправлялась каждые 15 мин без дедупликации + после сброса state бот пытался переотправить все 115 старых сделок | Дедупликация `last_large_loss_ts` + защита от state reset |
| 37 | **Бесконечный consec loss pause** — после истечения 30 мин паузы бот проверял те же 2 убыточные сделки → ставил НОВЫЙ pause. Бот застревал навсегда | После истечения pause: сброс `_consec_pause_until=0`, продолжение к strategy signal |
| 38 | Distance filter 1.5×ATR в strategy.py блокировал ВСЕ сильные тренды — при ADX=48 цена всегда далеко от EMA | Убран distance filter 1.5×ATR из strategy.py |
| 39 | **Бот не переподключался при истечении MCP сессии** — cTrader возвращал 404 на `tools/call`, бот печатал ошибки 3 часа но не reconect | Счётчик consecutive 404, после 3 → `reconnect()` автоматически |
| 40 | Pullback filter 1.0×ATR + daily range filter 0.7 блокировали входы на сильных трендах — distance 4.15×ATR и range_pos 96% блокировали LONG при ADX=50 | `PULLBACK_MAX_MULT=3.0` (была 1.0) + daily range `0.95` (было 0.7) |
| 41 | Cooldown после SL имел потолок 120 мин (2 часа) — слишком долго, бот пропускал возможности | Потолок уменьшен с 7200s (120m) до 3600s (60m). Эскалация: 30→60, max 60 мин |
| 42 | **Trailing SL работал с 1-го тика** — цена чуть вверх → SL подтянулся → цена развернулась → SL снесло. Сегодня 4 LONG: peak +$5, trailing подтянул SL с $3967 до $3976, цена развернулась и снесла. Без trailing позиции выжили бы | Trailing SL активируется только при PnL ≥ +0.4% (`TRAIL_ACTIVATE_PCT=0.4`). До этого SL стоит на initial (entry - 3×ATR) |
| 43 | **M30 EMA20 = 10 часов** — слишком короткая, реагировала на внутридневные откаты как на смену тренда. Сегодня M30 EMA20 упала с $4052 до $4049 на откате → бот решил "тренд вниз" и шортил на $4035 → цена развернулась вверх → -$393 | M30 EMA: 20 → **41** (20.5 часов ≈ 1 день). Также добавлен **daily trend filter**: price > today's H1 open → только LONG, price < open → только SHORT. Daily open берётся из первой H1 свечи MSK дня, логируется для бенчмарка |
| 44 | ADX_THRESHOLD=25 (hardcoded) + дублирующая проверка `adx < 20` в основном коде — блокировали входы при ADX 20-25, бот пропускал тренды | `ADX_THRESHOLD=20` в `.env` (configurable), дублирующая проверка убрана |
| 45 | **Trailing SL активировался при +0.4% — всё ещё слишком рано.** Peak +$19 (+0.47%), trailing подтянул SL с $4088 до $4111, цена развернулась → -$933. Без trailing SL остался бы на $4088, цена $4111 не задела бы | Trailing SL только **после BE** (0.5%): Stage 1 — SL initial, Stage 2 — BE → SL на entry, Stage 3 — после BE trailing следует за ценой |
| 46 | **M5 таймфрейм — ATR слишком маленькая ($5-11), SL = $15-33.** Стопы сносило на откатах постоянно. Сегодня SL $4111, лой $4101 — $10 разницы | Перешёл на **M15**: ATR $12.89, SL = $38.67. В 2-3× шире, переживает откаты |
| 47 | **TP1 закрывал позиции слишком рано — бот открывал новые по ХУДШЕЙ цене.** Сегодня: Entry $4149 → TP1 +$690, потом Entry $4176 → $0, Entry $4186 → open. Вместо +$2,052 если бы обе позиции открылись на $4149 | **2 входа одновременно**: Entry #1 (0.7 lot, TP) + Entry #2 (0.5 lot, без TP, rides trend). MAX_ENTRIES=2, ENTRY_VOLUMES=0.7,0.5 |

---

## 14. Paper боты (Hyperliquid, VPS)

| Бот | Version | State file | Status |
|---|---|---|---|
| GOLD | v10.2 | gold_bot_state_v10.2.json | 🟢 paper |
| BTC | v9 | btc_bot_state_v9.json | 🟡 paper |
| HYPE | v8.1 | hype_bot_state_v8.1.json | 🟡 paper |
| XYZ100 | v7 | xyz100_bot_state_v7.json | 🟡 paper |
| WTI | v9 | wti_bot_state_v7.json | 🟢 paper |

---

## 15. Pending tasks

1. **Мониторинг** — следить за метриками GOLD бота 5-7 дней
2. **PipFarm challenge** — купить $5K Classic ($25 с промо `2026`) когда метрики подтвердятся
3. **HL live $20** — когда вернёшься к этому (отложено)

---

## 16. История стратегий

### Стратегия v3: RSI + Stochastic (15 июля 2026, баг #56)

**Обновление 15 июля 19:30 MSK:** RSI пороги смягчены 20/80 → **25/75**.
**Обновление 15 июля 20:05 MSK:** RSI пороги смягчены 25/75 → **30/70** (по запросу пользователя). При 25/75 за 1.5 часа 0 входов (RSI за день колебался 29.1–69.3, ни разу не достиг ни 25 ни 75). С 30/70 ожидается 1-3 входа в день. Stoch 20/80 без изменений.
- **Файлы:** `strategy_rsi.py` (новая), `gold_mcp_bot_remote.py` (переписан)
- **Старая стратегия:** `strategy_old_ema.py` + `gold_mcp_bot_remote_old_ema.py` (бэкап на VPS)
- **Вход LONG:** RSI(14) < 30 AND Stochastic %K < 20 (M30 EMA41 rising/flat)  *(порог RSI: 20→25→30, 15 июля)*
- **Вход SHORT:** RSI(14) > 70 AND Stochastic %K > 80 (M30 EMA41 falling/flat)  *(порог RSI: 80→75→70, 15 июля)*
- **Выход:** RSI пересекает 50 (середина)
- **SL/TP:** Фиксированные $25/$35 (не ATR)
- **Объём:** 0.3 lot, один вход (без scale-in)
- **Max сделок:** 3 в день
- **Таймфрейм:** M15
- **Фильтры:** News filter ±15 мин, Friday close 23:40, daily loss 2.5%
- **M30 EMA41:** только для определения тренда, не для входа

### Стратегия v2: Adaptive (flip) (9-14 июля 2026, баги #51-55)
- EMA+ADX с переключением reversion/momentum по ADX
- Результат: -$13,108 (слив)
- Причина: EMA запаздывает, покупает на хаях, продаёт на лоях

### Стратегия v1: EMA+ADX momentum (26 июня - 8 июля 2026, баги #1-50)
- EMA20→14 + ADX + ATR на M5→M15
- Результат: -$5,328 (слив)
- Причина: та же — momentum входит на экстремумах

- **Адаптивный переключатель тренд/боковик — ВНЕДРЁН 9 июля (баг #52):**
  - ADX < 25 → боковик → **mean reversion** (flip): цена > EMA → SHORT, цена < EMA → LONG
  - ADX ≥ 25 → тренд → **momentum**: цена > EMA → LONG, цена < EMA → SHORT
  - Фильтры (M30, daily open) тоже переключаются по режиму
  - **GOLD**: strategy.py + gold_mcp_bot_remote.py — adaptive ✅
  - **BTC**: strategy.py — adaptive (REVERSE_SIGNAL больше не используется, заменён на ADX switch) ✅
- Остановить HL paper bots (освободит 20% CPU)
- Telegram bot команды (/status, /trades, /pause)
- EURUSD бот (EMA50, ADX20, SL1.5×ATR, TP2=5×ATR)

---

## 17. Локальные файлы (/home/z/my-project/)

- `BOT_CONTEXT.md` — ЭТОТ файл (единственный источник правды)
- `MY_RESPONSIBILITY.md` — правила "не менять без разрешения"
- `scripts/gold_mcp_bot_remote.py` — локальная копия бота
- `scripts/ctrader_alerts.py` — Telegram alerts
- `scripts/ssh_helper.py` — SSH wrapper
- `scripts/analyze_trades.py` — анализ сделок

---

## TL;DR для нового чата

«2 live бота на cTrader Remote MCP (GOLD $100K +$3,082 + BTC $5K) + 5 paper ботов на Hyperliquid. Live боты работают 24/7 на Linux VPS 193.233.19.171, без cTrader Desktop. Стратегия: EMA14+ADX+ATR (была EMA20, изменена 01.07), scale-in 3 entries, SL=3×ATR, TP2=4×ATR, per-entry trailing SL, M30 trend filter, pullback filter 3.0×ATR, BE 0.5%, consec loss pause 30 мин, cooldown 30→60 мин (max 60). Volume formula: new_volume = current × (new_balance/old_balance) × (old_lev/new_lev). relativeStopLoss/TakeProfit в PIPETTES, rounded to 1000 (XAU) / 100 (BTC). Dashboard: http://193.233.19.171:8080/report_latest.html. Telegram: @AITradingAlertPNLBot. GitHub: github.com/impowery/Goldctraderbot. SSH: root@193.233.19.171 (пароль в Notes). Контекст: BOT_CONTEXT.md (единственный файл, дубликатов нет).»

---

## Правила (кратко — полный текст в MY_RESPONSIBILITY.md)

1. **БЕЗ РАЗРЕШЕНИЯ ПОЛЬЗОВАТЕЛЯ НЕ МЕНЯТЬ НИ ОДНОГО ИЗМЕНЕНИЯ** в коде/.env/конфигах
2. **GOLD бот — наш. BTC бот — НЕ ТРОГАТЬ.**
3. **После ЛЮБЫХ правок — перезапустить сервис и проверить в логе**
4. **Никогда не говорить "всё в порядке" без проверки реальных сделок**
---

## 18. История сделок и отчётов (добавлено 21 июля 2026)

### 18.1 Как посмотреть историю сделок

**На VPS — быстро через скрипт:**
```bash
# GOLD — сводка + последние 20 сделок
python3 /root/bots/view_trades.py --bot gold

# BTC — только сводка по дням
python3 /root/bots/view_trades.py --bot btc --summary

# GOLD — сделки за сегодня
python3 /root/bots/view_trades.py --bot gold --today

# BTC — сделки с 15 июля
python3 /root/bots/view_trades.py --bot btc --since 2026-07-15

# Экспорт GOLD в CSV (для Excel/Google Sheets)
python3 /root/bots/view_trades.py --bot gold --csv /tmp/gold_trades.csv

# Последние 50 сделок BTC
python3 /root/bots/view_trades.py --bot btc -n 50
```

**Сырые JSONL файлы** (для анализа/парсинга):
- `/root/bots/trades_gold_ctrader.jsonl` — все сделки GOLD (1 строка = 1 сделка JSON)
- `/root/bots/trades_btc_ctrader.jsonl` — все сделки BTC
- `/root/bots/trades_gold.jsonl` — старые сделки GOLD (paper bot)
- `/root/bots/trades_btc.jsonl` — старые сделки BTC (paper bot)
- `/root/bots/trades_hype.jsonl`, `trades_xyz100.jsonl`, `trades_wti.jsonl` — paper bots

**Формат JSONL:**
```json
{
  "ts": "2026-07-21T11:04:52Z",
  "type": "SHORT",
  "entry_price": 4061.94,
  "exit_price": 4056.73,
  "pnl": 378.91,
  "reason": "TIME",
  "entries": 1,
  "version": "ctrader-backfill",
  "position_id": 278020646,
  "volume_lots": 0.7
}
```

### 18.2 История отчётов (HTML dashboards)

**На VPS:**
- `/root/bots/reports/` — последние 7 дней HTML отчётов (каждые 5 мин, ~94 KB каждый)
- `/root/bots/report_latest.html` — самый свежий отчёт (обновляется каждые 5 мин)
- `/root/bots/reports_index.json` — индекс всех HTML на диске + архивов
- `/root/bots/reports_archive/` — SHA256 checksums архивов (сами архивы загружены в GitHub)

**В GitHub Releases (вся история навсегда):**
https://github.com/impowery/Goldctraderbot/releases

Теги: `reports-2026-W23`, `reports-2026-W24`, ..., `reports-2026-W29` (и далее автоматически каждую неделю)

Структура архива: `<week_key>/<YYYY-MM-DD>/report_<YYYYMMDD>_<HHMMSS>.html`

**Скачать конкретную неделю:**
```bash
wget https://github.com/impowery/Goldctraderbot/releases/download/reports-2026-W29/reports_2026-W29.tar.gz
tar xzf reports_2026-W29.tar.gz
# открой: 2026-W29/2026-07-15/report_20260715_120001.html
```

**Manifest со всеми URL'ами:** `/root/bots/reports_manifest.json`

### 18.3 Автоматизация (cron)

**Cron job (3:00 UTC ежедневно):**
```
0 3 * * * /root/bots/archive_and_upload.sh >> /root/bots/logs/archive_reports.log 2>&1
```

Скрипт `archive_and_upload.sh`:
1. Архивирует HTML отчёты старше 7 дней в `reports_YYYY-Www.tar.gz` (gzip, -86%)
2. Загружает архивы в GitHub Releases (бесконечное бесплатное хранилище)
3. Удаляет локальные архивы после upload (оставляет SHA256 для верификации)
4. Перестраивает `reports_index.json`

### 18.4 Скрипты на VPS

| Скрипт | Назначение |
|---|---|
| `/root/bots/gen_report.py` | Генерация HTML отчёта каждые 5 мин (cron) |
| `/root/bots/dashboard_server.py` | HTTP сервер дашборда на порту 8080 |
| `/root/bots/archive_reports.py` | Архивация старых отчётов в tar.gz |
| `/root/bots/upload_reports_to_github.py` | Upload архивов в GitHub Releases |
| `/root/bots/archive_and_upload.sh` | Wrapper для cron (archive + upload + index) |
| `/root/bots/view_trades.py` | Просмотр истории сделок (сводка, фильтры, CSV export) |

### 18.5 Доступ к дашборду

**URL:** http://193.233.19.171:8080/

**Что показывает:**
- Live метрики всех ботов (баланс, equity, PnL)
- Текущие позиции
- Графики PnL за день/неделю
- Обновление каждые 5 минут

**Команды управления дашбордом:**
```bash
# Проверить что работает
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/  # должно быть 200

# Перезапустить если завис
ps -ef | grep dashboard_server | grep -v grep
kill <PID>
cd /root/bots && nohup python3 dashboard_server.py > /root/bots/dashboard.log 2>&1 &
```

### 18.6 Сохранять ли на ноутбук?

**Нет, не обязательно.** Вся история уже в 3 местах:
1. **VPS** — последние 7 дней HTML + все JSONL trade logs
2. **GitHub Releases** — все HTML отчёты навсегда (по неделям)
3. **GitHub repo** — текущий код ботов

**Если нужно скачать на ноут:**
```bash
# Все сделки GOLD в CSV
ssh root@193.233.19.171 'python3 /root/bots/view_trades.py --bot gold --csv /tmp/gold.csv'
scp root@193.233.19.171:/tmp/gold.csv ~/Desktop/

# Или весь trade log JSONL
scp root@193.233.19.171:/root/bots/trades_gold_ctrader.jsonl ~/Desktop/

# Скачать все архивы отчётов из GitHub (одной командой)
for w in W23 W24 W25 W26 W27 W28 W29; do
  wget https://github.com/impowery/Goldctraderbot/releases/download/reports-2026-$w/reports_2026-$w.tar.gz
done
```

**Регулярный backup на ноут (опционально, раз в месяц):**
```bash
# Создать архив всех trade logs
ssh root@193.233.19.171 'tar czf /tmp/all_trades.tar.gz /root/bots/trades_*.jsonl'
scp root@193.233.19.171:/tmp/all_trades.tar.gz ~/Backups/trades_$(date +%Y%m%d).tar.gz
```

### 18.7 Сводка по местам хранения

| Что | Где | Размер | Давность |
|---|---|---|---|
| HTML отчёты (свежие) | VPS `/root/bots/reports/` | ~180 MB | последние 7 дней |
| HTML отчёты (архив) | GitHub Releases | ~120 MB | навсегда |
| Trade logs JSONL | VPS `/root/bots/trades_*.jsonl` | ~500 KB total | навсегда |
| State files | VPS bot dirs | ~1 KB каждый | текущее состояние |
| Bot code | VPS + GitHub repos | ~200 KB | текущий + git history |
| Manifest | VPS `/root/bots/reports_manifest.json` | 4 KB | обновляется автоматически |
| Index | VPS `/root/bots/reports_index.json` | 428 KB | обновляется автоматически |

