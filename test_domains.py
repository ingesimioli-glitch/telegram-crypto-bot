import httpx
import asyncio

async def test_ens(name: str):
    url = f"https://ensdata.net/{name}"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(url, timeout=5.0)
            print(f"ENS {name} status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                print(f"ENS resolved address: {data.get('address')}")
            else:
                print(f"ENS failed: {response.text}")
        except Exception as e:
            print(f"ENS error: {e}")

async def test_sns(name: str):
    # Try sdk-proxy.sns.id with the clean name (removing .sol)
    clean_name = name.replace(".sol", "")
    url = f"https://sdk-proxy.sns.id/resolve/{clean_name}"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(url, timeout=5.0)
            print(f"SNS {name} status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                print(f"SNS resolved address (clean): {data.get('result')}")
            else:
                print(f"SNS failed: {response.text}")
        except Exception as e:
            print(f"SNS error: {e}")

async def main():
    print("Testing ENS resolution...")
    await test_ens("vitalik.eth")
    print("\nTesting SNS resolution...")
    await test_sns("bonfida.sol")

if __name__ == "__main__":
    asyncio.run(main())
