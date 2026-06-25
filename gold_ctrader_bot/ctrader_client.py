"""cTrader Open API client using ctrader-api-client (asyncio)."""

import asyncio
from ctrader_api_client import CTraderClient, SpotEvent, ReadyEvent
from ctrader_api_client.models import NewOrderRequest, ClosePositionRequest
from ctrader_api_client.enums import OrderSide, OrderType

from settings import *


class GoldBotClient:
    def __init__(self):
        self.client = CTraderClient({
            "client_id": CTRADER_CLIENT_ID,
            "client_secret": CTRADER_CLIENT_SECRET,
        })
        self.account_id = CTRADER_ACCOUNT_ID
        self.symbol_id = None
        self.last_trade_minute = 0
        self.price_history: list[float] = []
        self.high_history: list[float] = []
        self.low_history: list[float] = []

    async def start(self):
        self.client.on(SpotEvent, symbol_name=SYMBOL_NAME)(self.on_price)
        self.client.on(ReadyEvent)(self.on_ready)

        async with self.client:
            await self.client.auth.authenticate_app()
            await self.client.auth.authenticate_by_trader_login(
                trader_login=self.account_id,
                access_token=CTRADER_ACCESS_TOKEN,
                refresh_token=CTRADER_REFRESH_TOKEN,
                expires_at=0,
            )
            await asyncio.Event().wait()

    async def on_ready(self, event: ReadyEvent):
        symbols = await self.client.market_data.get_symbols(event.account_id)
        for s in symbols:
            if s.name == SYMBOL_NAME:
                self.symbol_id = s.id
                break
        if self.symbol_id:
            await self.client.market_data.subscribe_spots(event.account_id, [self.symbol_id])

    async def on_price(self, event: SpotEvent):
        if event.ctid_trader_account_id != self.account_id:
            return
        price = (event.bid + event.ask) / 2
        self.price_history.append(price)
        self.high_history.append(max(event.bid, event.ask))
        self.low_history.append(min(event.bid, event.ask))
        if len(self.price_history) > 100:
            self.price_history.pop(0)
            self.high_history.pop(0)
            self.low_history.pop(0)

        positions = await self.client.trading.get_positions(self.account_id)
        has_pos = any(
            p.symbol_id == self.symbol_id
            for p in (positions or [])
        )

        from strategy import should_enter
        enter, reason = should_enter(self.price_history, self.high_history, self.low_history)

        if enter and not has_pos:
            direction = OrderSide.BUY if "LONG" in reason else OrderSide.SELL
            vol = int(TRADE_VOLUME * 100_000)
            req = NewOrderRequest(
                symbol_id=self.symbol_id,
                order_type=OrderType.MARKET,
                side=direction,
                volume=vol,
                stop_loss_pips=STOP_LOSS_PIPS,
                take_profit_pips=TAKE_PROFIT_PIPS,
            )
            result = await self.client.trading.create_order(self.account_id, req)
            print(f"[TRADE] {reason} | order_id={result}")
