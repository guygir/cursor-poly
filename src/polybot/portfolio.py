from __future__ import annotations

from polybot.polymarket_client import PolymarketClient


class Portfolio:
    def __init__(self, client: PolymarketClient, user_address: str) -> None:
        self._client = client
        self._user_address = user_address

    def held_shares(self, token_id: str, market_id: str | None = None) -> float:
        position = self._client.get_position(
            user_address=self._user_address,
            token_id=token_id,
            market_id=market_id,
        )
        return position.size
