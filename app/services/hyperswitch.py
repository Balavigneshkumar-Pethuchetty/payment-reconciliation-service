import httpx

from app.config import settings


class HyperswitchClient:
    def __init__(self):
        self.base_url = settings.HYPERSWITCH_BASE_URL.rstrip("/")
        self.headers = {
            "api-key": settings.HYPERSWITCH_API_KEY,
            "Content-Type": "application/json",
        }

    async def create_payment(
        self,
        amount_in_paise: int,
        currency: str,
        transaction_id: str,
        metadata: dict,
    ) -> dict:
        payload = {
            "amount": amount_in_paise,
            "currency": currency,
            "description": f"Payment {transaction_id}",
            "payment_method": "upi",
            "metadata": metadata,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/payments",
                json=payload,
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def confirm_payment(self, payment_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.base_url}/payments/{payment_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()


hyperswitch_client = HyperswitchClient()
