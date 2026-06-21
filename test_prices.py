import asyncio
import httpx

async def main():
    symbols = ["ETHUSDT", "SOLUSDT", "BNBUSDT", "POLUSDT", "MATICUSDT"]
    async with httpx.AsyncClient() as client:
        for sym in symbols:
            try:
                r = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}")
                print(f"{sym}: {r.status_code} -> {r.text}")
            except Exception as e:
                print(f"{sym} error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
