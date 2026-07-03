"""Free market-data feed (Binance + alternative.me + CoinGecko) that replaces
the paid CoinMarketCap Pro subscription for signals and market regime.

Valuation of holdings is NOT here — it is done on-chain via the PancakeSwap
execution client, so the mark matches what a sell would realize."""
from agent.market.feed import MarketFeed

__all__ = ["MarketFeed"]
