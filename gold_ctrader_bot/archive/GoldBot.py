import clr
clr.AddReference("cAlgo.API")
from cAlgo.API import *
from robot_wrapper import *


class GoldBot():

    def on_start(self):
        self.ema = api.Indicators.ExponentialMovingAverage(
            api.Bars.ClosePrices, self.get_parameter("ema_period", 20))
        self.adx = api.Indicators.AverageDirectionalMovementIndex(
            api.Bars.HighPrices, api.Bars.LowPrices, api.Bars.ClosePrices, self.get_parameter("adx_period", 14))
        self.atr = api.Indicators.AverageTrueRange(
            api.Bars.HighPrices, api.Bars.LowPrices, api.Bars.ClosePrices, self.get_parameter("atr_period", 14))

        self.volume = self.get_parameter("volume", 0.01)
        self.sl_pips = self.get_parameter("stop_loss", 200)
        self.tp_pips = self.get_parameter("take_profit", 400)
        self.adx_threshold = self.get_parameter("adx_threshold", 25)
        self.min_interval = self.get_parameter("min_interval", 60)
        self.max_daily_loss = self.get_parameter("max_daily_loss", 3.0)
        self.ema_period = self.get_parameter("ema_period", 20)

        self.last_trade_time = None
        self.daily_start_balance = api.Account.Balance
        self.daily_loss_hit = False

        api.Print(f"GoldBot started | {api.SymbolName} | Balance={api.Account.Balance}")

    def on_bar(self):
        # Daily loss check
        daily_pnl = api.Account.Balance - self.daily_start_balance
        daily_limit = -self.daily_start_balance * (self.max_daily_loss / 100)
        if daily_pnl < daily_limit:
            if not self.daily_loss_hit:
                api.Print(f"Daily loss {daily_pnl:.2f} < {daily_limit:.2f} — paused")
                self.daily_loss_hit = True
            pos = api.Positions.Find(api.SymbolName)
            if pos is not None:
                api.ClosePosition(pos)
            return

        # New day reset
        day = api.Server.Time.Day
        if not hasattr(self, "_day") or self._day != day:
            self._day = day
            self.daily_start_balance = api.Account.Balance
            self.daily_loss_hit = False

        # Min interval
        if self.last_trade_time is not None:
            elapsed = (api.Server.Time - self.last_trade_time).TotalMinutes
            if elapsed < self.min_interval:
                return

        idx = api.Bars.Count - 1
        if idx < self.adx_period * 3:
            return

        ema_val = self.ema.Result[idx]
        adx_val = self.adx.Result[idx]
        price = api.Bars.ClosePrices[idx]

        if adx_val < self.adx_threshold:
            return

        # Check open position
        if api.Positions.Find(api.SymbolName) is not None:
            return

        # Direction
        direction = TradeType.Buy if price > ema_val else TradeType.Sell
        label = "LONG" if direction == TradeType.Buy else "SHORT"

        volume = int(self.volume * 100000)
        result = api.ExecuteMarketOrder(
            direction, api.SymbolName, volume, label, self.sl_pips, self.tp_pips)

        if result.IsSuccessful:
            self.last_trade_time = api.Server.Time
            api.Print(f"{label} @ {price:.2f} | EMA={ema_val:.2f} ADX={adx_val:.1f}")
        else:
            api.Print(f"Order failed: {result.Error}")

    def on_tick(self):
        pass

    def on_stop(self):
        api.Print("GoldBot stopped")

    def get_parameter(self, name, default):
        try:
            return int(api.GetParameter(name))
        except:
            try:
                return float(api.GetParameter(name))
            except:
                return default
