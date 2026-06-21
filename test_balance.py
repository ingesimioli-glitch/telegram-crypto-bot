import asyncio
import re
import httpx
import base58
import os
from dotenv import load_dotenv

load_dotenv()

# Regex patterns
EVM_REGEX = re.compile(r'\b0x[a-fA-F0-9]{40}\b')
SOLANA_REGEX = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')

# RPC URLs (with defaults)
RPC_CONFIG = {
    "EVM": {
        "Ethereum (ETH)": os.getenv("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com"),
        "BSC (BNB)": os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org/"),
        "Polygon (POL)": os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
        "Arbitrum (ETH)": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"),
        "Optimism (ETH)": os.getenv("OPTIMISM_RPC_URL", "https://mainnet.optimism.io"),
    },
    "Solana": {
        "Solana (SOL)": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    }
}

def is_valid_solana(address: str) -> bool:
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False

def extract_addresses(text: str):
    evm_candidates = EVM_REGEX.findall(text)
    # Filter unique EVM addresses
    evm_addresses = sorted(list(set(evm_candidates)))
    
    sol_candidates = SOLANA_REGEX.findall(text)
    sol_addresses = []
    for cand in sol_candidates:
        if is_valid_solana(cand) and cand not in sol_addresses:
            sol_addresses.append(cand)
            
    return evm_addresses, sol_addresses

async def fetch_evm_balance(client: httpx.AsyncClient, chain_name: str, rpc_url: str, address: str) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1
    }
    try:
        response = await client.post(rpc_url, json=payload, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        if "result" in data:
            wei = int(data["result"], 16)
            balance = wei / 10**18
            return {"chain": chain_name, "balance": balance, "success": True}
        else:
            error_msg = data.get("error", {}).get("message", "Unknown RPC error")
            return {"chain": chain_name, "error": error_msg, "success": False}
    except Exception as e:
        return {"chain": chain_name, "error": str(e), "success": False}

async def fetch_solana_balance(client: httpx.AsyncClient, rpc_url: str, address: str) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": "getBalance",
        "params": [address],
        "id": 1
    }
    try:
        response = await client.post(rpc_url, json=payload, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        if "result" in data and "value" in data["result"]:
            lamports = data["result"]["value"]
            balance = lamports / 10**9
            return {"chain": "Solana (SOL)", "balance": balance, "success": True}
        else:
            error_msg = data.get("error", {}).get("message", "Unknown RPC error")
            return {"chain": "Solana (SOL)", "error": error_msg, "success": False}
    except Exception as e:
        return {"chain": "Solana (SOL)", "error": str(e), "success": False}

async def check_balances(address: str, is_evm: bool):
    async with httpx.AsyncClient() as client:
        if is_evm:
            tasks = [
                fetch_evm_balance(client, chain, url, address)
                for chain, url in RPC_CONFIG["EVM"].items()
            ]
            results = await asyncio.gather(*tasks)
            return results
        else:
            sol_rpc = RPC_CONFIG["Solana"]["Solana (SOL)"]
            result = await fetch_solana_balance(client, sol_rpc, address)
            return [result]

def format_balance(val: float) -> str:
    if val == 0:
        return "0"
    elif val < 0.0001:
        return f"{val:.6f}"
    elif val < 1.0:
        return f"{val:.4f}"
    else:
        return f"{val:.2f}"

async def main():
    test_text = """
    Привет, проверь плиз эти кошельки:
    EVM: 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 (это Виталик)
    Solana: 7Y22dM25CThx8b2AfdE1tHSpjFjAyn65W1fbf5TqP6t7 (какой-то солана адрес)
    И еще один невалидный солана адрес: invalidSOLaddress12345
    """
    print("Parsing text...")
    evm_addrs, sol_addrs = extract_addresses(test_text)
    print(f"Detected EVM addresses: {evm_addrs}")
    print(f"Detected Solana addresses: {sol_addrs}")
    
    for addr in evm_addrs:
        print(f"\nChecking EVM address: {addr}...")
        balances = await check_balances(addr, is_evm=True)
        for res in balances:
            if res["success"]:
                print(f"  - {res['chain']}: {format_balance(res['balance'])}")
            else:
                print(f"  - {res['chain']}: Error ({res['error']})")
                
    for addr in sol_addrs:
        print(f"\nChecking Solana address: {addr}...")
        balances = await check_balances(addr, is_evm=False)
        for res in balances:
            if res["success"]:
                print(f"  - {res['chain']}: {format_balance(res['balance'])}")
            else:
                print(f"  - {res['chain']}: Error ({res['error']})")

if __name__ == "__main__":
    asyncio.run(main())
