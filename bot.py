import asyncio
import logging
import os
import re
import time
import html
import httpx
import base58
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logging.warning("TELEGRAM_BOT_TOKEN is not set. Please set it in your .env file.")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Regex patterns for wallet and domain detection
EVM_REGEX = re.compile(r'\b0x[a-fA-F0-9]{40}\b')
SOLANA_REGEX = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
ENS_REGEX = re.compile(r'\b(?:[a-zA-Z0-9-]+\.)+eth\b')
SNS_REGEX = re.compile(r'\b(?:[a-zA-Z0-9-_]+\.)+sol\b')

# RPC Configurations for Native Balances
RPC_CONFIG = {
    "EVM": {
        "Ethereum (ETH)": {"url": os.getenv("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com"), "token": "ETH"},
        "BSC (BNB)": {"url": os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org/"), "token": "BNB"},
        "Polygon (POL)": {"url": os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"), "token": "POL"},
        "Arbitrum (ETH)": {"url": os.getenv("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc"), "token": "ETH"},
        "Optimism (ETH)": {"url": os.getenv("OPTIMISM_RPC_URL", "https://mainnet.optimism.io"), "token": "ETH"},
    },
    "Solana": {
        "Solana (SOL)": {"url": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"), "token": "SOL"}
    }
}

# Blockscout endpoints for fetching ERC-20 tokens
BLOCKSCOUT_CONFIG = {
    "Ethereum": "https://eth.blockscout.com",
    "Arbitrum": "https://arbitrum.blockscout.com",
    "Optimism": "https://optimism.blockscout.com",
    "Polygon": "https://polygon.blockscout.com",
    "BSC": "https://blockscout.com/bsc/mainnet",
}

# Price Caching
PRICE_CACHE = {
    "prices": {},
    "last_fetched": 0
}

# Initialize bot and dispatcher
bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()

def is_valid_solana(address: str) -> bool:
    """Validate if the string is a valid Solana public key by decoding base58."""
    try:
        decoded = base58.b58decode(address)
        return len(decoded) == 32
    except Exception:
        return False

def extract_addresses_and_domains(text: str):
    """Extract unique EVM, Solana addresses and ENS, SNS domains from text."""
    # EVM Addresses
    evm_candidates = EVM_REGEX.findall(text)
    evm_dict = {}
    for cand in evm_candidates:
        evm_dict[cand.lower()] = cand
    evm_addresses = list(evm_dict.values())
    
    # Solana Addresses
    sol_candidates = SOLANA_REGEX.findall(text)
    sol_addresses = []
    for cand in sol_candidates:
        if is_valid_solana(cand) and cand not in sol_addresses:
            sol_addresses.append(cand)
            
    # ENS Domains
    ens_candidates = ENS_REGEX.findall(text)
    ens_domains = sorted(list(set([d.lower() for d in ens_candidates])))
    
    # SNS Domains
    sns_candidates = SNS_REGEX.findall(text)
    sns_domains = sorted(list(set([d.lower() for d in sns_candidates])))
            
    return evm_addresses, sol_addresses, ens_domains, sns_domains

async def get_token_prices() -> dict:
    """Fetch current prices for ETH, BNB, POL, SOL from Binance and cache them for 5 minutes."""
    now = time.time()
    if PRICE_CACHE["prices"] and (now - PRICE_CACHE["last_fetched"] < 300):
        return PRICE_CACHE["prices"]
        
    prices = {"ETH": 1700.0, "BNB": 580.0, "POL": 0.38, "SOL": 70.0}  # Fallbacks
    symbols = {
        "ETH": "ETHUSDT",
        "BNB": "BNBUSDT",
        "POL": "MATICUSDT",  # Using MATIC price for Polygon POL
        "SOL": "SOLUSDT"
    }
    async with httpx.AsyncClient() as client:
        for token, symbol in symbols.items():
            try:
                response = await client.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=3.0)
                if response.status_code == 200:
                    data = response.json()
                    prices[token] = float(data["price"])
            except Exception as e:
                logging.warning(f"Failed to fetch price for {token} ({symbol}): {e}")
                
    PRICE_CACHE["prices"] = prices
    PRICE_CACHE["last_fetched"] = now
    return prices

async def resolve_ens(client: httpx.AsyncClient, domain: str) -> str:
    """Resolve an ENS name (.eth) to an Ethereum address using ensdata.net."""
    url = f"https://ensdata.net/{domain}"
    try:
        response = await client.get(url, timeout=5.0)
        if response.status_code == 200:
            data = response.json()
            addr = data.get("address")
            if addr and EVM_REGEX.match(addr):
                return addr
    except Exception as e:
        logging.warning(f"Error resolving ENS domain {domain}: {e}")
    return None

async def resolve_sns(client: httpx.AsyncClient, domain: str) -> str:
    """Resolve an SNS name (.sol) to a Solana address using sdk-proxy.sns.id."""
    clean_name = domain.replace(".sol", "")
    url = f"https://sdk-proxy.sns.id/resolve/{clean_name}"
    try:
        response = await client.get(url, timeout=5.0)
        if response.status_code == 200:
            data = response.json()
            if data.get("s") == "ok":
                addr = data.get("result")
                if addr and is_valid_solana(addr):
                    return addr
    except Exception as e:
        logging.warning(f"Error resolving SNS domain {domain}: {e}")
    return None

async def fetch_evm_balance(client: httpx.AsyncClient, chain_name: str, rpc_url: str, token_symbol: str, address: str) -> dict:
    """Fetch EVM balance for a given address and chain."""
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
            return {"chain": chain_name, "balance": balance, "token": token_symbol, "success": True}
        else:
            error_msg = data.get("error", {}).get("message", "Unknown RPC error")
            return {"chain": chain_name, "error": error_msg, "token": token_symbol, "success": False}
    except Exception as e:
        logging.warning(f"Error fetching EVM balance on {chain_name} for {address}: {e}")
        return {"chain": chain_name, "error": str(e), "token": token_symbol, "success": False}

async def fetch_solana_balance(client: httpx.AsyncClient, rpc_url: str, address: str) -> dict:
    """Fetch Solana balance for a given address."""
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
            bytes_val = data["result"]["value"]
            balance = bytes_val / 10**9
            return {"chain": "Solana (SOL)", "balance": balance, "token": "SOL", "success": True}
        else:
            error_msg = data.get("error", {}).get("message", "Unknown RPC error")
            return {"chain": "Solana (SOL)", "error": error_msg, "token": "SOL", "success": False}
    except Exception as e:
        logging.warning(f"Error fetching Solana balance for {address}: {e}")
        return {"chain": "Solana (SOL)", "error": str(e), "token": "SOL", "success": False}

async def fetch_blockscout_tokens(client: httpx.AsyncClient, chain_name: str, base_url: str, address: str) -> list:
    """Fetch all ERC-20 token balances for a wallet address from Blockscout API."""
    url = f"{base_url}/api/v2/addresses/{address}/token-balances"
    try:
        response = await client.get(url, timeout=8.0)
        if response.status_code == 200:
            data = response.json()
            tokens = []
            for item in data:
                token_info = item.get("token")
                if not token_info or token_info.get("type") != "ERC-20":
                    continue
                value = item.get("value")
                if not value or value == "0":
                    continue
                
                decimals = int(token_info.get("decimals") or "18")
                balance = float(value) / 10**decimals
                price = float(token_info.get("exchange_rate") or 0.0)
                usd_value = balance * price
                
                # Filter out dust (< $0.50)
                if usd_value > 0.50:
                    tokens.append({
                        "name": token_info.get("name", "Unknown"),
                        "symbol": token_info.get("symbol", "UNKNOWN"),
                        "balance": balance,
                        "usd_value": usd_value,
                        "chain": chain_name
                    })
            return tokens
    except Exception as e:
        logging.warning(f"Error fetching blockscout tokens on {chain_name}: {e}")
    return []

async def check_balances(address: str, is_evm: bool):
    """Check balances for an address concurrently using HTTPX AsyncClient."""
    async with httpx.AsyncClient() as client:
        if is_evm:
            tasks = [
                fetch_evm_balance(client, chain, config["url"], config["token"], address)
                for chain, config in RPC_CONFIG["EVM"].items()
            ]
            return await asyncio.gather(*tasks)
        else:
            sol_rpc = RPC_CONFIG["Solana"]["Solana (SOL)"]["url"]
            result = await fetch_solana_balance(client, sol_rpc, address)
            return [result]

def format_balance(val: float) -> str:
    """Format float balance representation for readability."""
    if val == 0:
        return "0"
    elif val < 0.0001:
        return f"{val:.6f}"
    elif val < 1.0:
        return f"{val:.4f}"
    else:
        return f"{val:.2f}"

async def process_custom_address_check(address: str, chat_id: int):
    """Fetch balances and tokens for a specific address, format report, send it to a chat, and optionally pin it."""
    try:
        # Classify address type
        is_evm = EVM_REGEX.match(address) is not None
        is_sol = is_valid_solana(address)
        
        # Resolve domains if needed
        if not is_evm and not is_sol:
            if address.endswith(".eth"):
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    resolved = await resolve_ens(client, address)
                    if resolved:
                        address = resolved
                        is_evm = True
            elif address.endswith(".sol"):
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    resolved = await resolve_sns(client, address)
                    if resolved:
                        address = resolved
                        is_sol = True
                        
        if not is_evm and not is_sol:
            logging.warning(f"Custom check failed: invalid address format for '{address}'")
            return
            
        prices = await get_token_prices()
        
        if is_evm:
            # Check EVM balances
            async with httpx.AsyncClient(follow_redirects=True) as client:
                native_task = check_balances(address, is_evm=True)
                token_tasks = [
                    fetch_blockscout_tokens(client, chain, base_url, address)
                    for chain, base_url in BLOCKSCOUT_CONFIG.items()
                ]
                native_balances, *tokens_results = await asyncio.gather(native_task, *token_tasks)
                
            native_lines = []
            total_usd = 0.0
            
            for res in native_balances:
                if res["success"]:
                    val = res["balance"]
                    val_str = format_balance(val)
                    token = res["token"]
                    price = prices.get(token, 0.0)
                    usd_value = val * price
                    total_usd += usd_value
                    
                    usd_str = f" (~${usd_value:,.2f})" if usd_value > 0.01 else ""
                    native_lines.append(f"• <b>{html.escape(res['chain'])}</b>: <code>{val_str} {html.escape(token)}</code>{usd_str}")
                else:
                    native_lines.append(f"• <b>{html.escape(res['chain'])}</b>: <i>Ошибка RPC</i>")
                    
            all_tokens = []
            for list_of_tokens in tokens_results:
                all_tokens.extend(list_of_tokens)
                
            for t in all_tokens:
                total_usd += t["usd_value"]
                
            all_tokens = sorted(all_tokens, key=lambda x: x["usd_value"], reverse=True)
            token_lines = []
            for t in all_tokens[:8]:
                val_str = format_balance(t["balance"])
                token_lines.append(f"• <b>{html.escape(t['symbol'])}</b> ({html.escape(t['chain'])}): <code>{val_str} {html.escape(t['symbol'])}</code> (~${t['usd_value']:,.2f})")
                
            if len(all_tokens) > 8:
                token_lines.append(f"<i>...и еще {len(all_tokens) - 8} токенов на общую сумму ${sum(t['usd_value'] for t in all_tokens[8:]):,.2f} USD</i>")
                
            balance_summary = "\n".join(native_lines)
            token_summary = "\n".join(token_lines)
            
            reply_text = (
                f"🔍 <b>EVM Wallet Summary</b>\n"
                f"Адрес: <code>{html.escape(address)}</code>\n\n"
                f"💰 <b>Нативные балансы:</b>\n"
                f"{balance_summary}\n\n"
            )
            if token_lines:
                reply_text += (
                    f"🪙 <b>Токены (Топ-8 по ценности):</b>\n"
                    f"{token_summary}\n\n"
                )
            reply_text += f"💵 <b>Общая стоимость:</b> <b>${total_usd:,.2f} USD</b>"
            
            should_pin = total_usd > 100.0
            if should_pin:
                reply_text += f"\n\n🔗 <a href=\"https://etherscan.io/address/{address}\">Смотреть на Etherscan</a>"
                
            try:
                sent_message = await bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                if should_pin:
                    try:
                        await sent_message.pin()
                    except Exception as pin_err:
                        logging.warning(f"Could not pin custom EVM message: {pin_err}")
            except Exception as e:
                logging.error(f"Failed to send custom EVM message to {chat_id} with HTML: {e}")
                try:
                    plain_text = re.sub(r'<[^>]+>', '', reply_text)
                    await bot.send_message(chat_id=chat_id, text=plain_text)
                except Exception as fallback_err:
                    logging.error(f"Failed to send fallback custom EVM message to {chat_id}: {fallback_err}")
                
        elif is_sol:
            # Check Solana balances
            balances = await check_balances(address, is_evm=False)
            lines = []
            total_usd = 0.0
            for res in balances:
                if res["success"]:
                    val = res["balance"]
                    val_str = format_balance(val)
                    token = res["token"]
                    price = prices.get(token, 0.0)
                    usd_value = val * price
                    total_usd += usd_value
                    
                    usd_str = f" (~${usd_value:,.2f})" if usd_value > 0.01 else ""
                    lines.append(f"• <b>{html.escape(res['chain'])}</b>: <code>{val_str} {html.escape(token)}</code>{usd_str}")
                else:
                    lines.append(f"• <b>{html.escape(res['chain'])}</b>: <i>Ошибка RPC</i>")
                    
            balance_summary = "\n".join(lines)
            reply_text = (
                f"🔍 <b>Solana Wallet Summary</b>\n"
                f"Адрес: <code>{html.escape(address)}</code>\n\n"
                f"💰 <b>Баланс:</b>\n"
                f"{balance_summary}\n\n"
                f"💵 <b>Общая стоимость:</b> <b>${total_usd:,.2f} USD</b>"
            )
            
            should_pin = total_usd > 11.0
            if should_pin:
                reply_text += f"\n\n🔗 <a href=\"https://solscan.io/account/{address}\">Смотреть на Solscan</a>"
                
            try:
                sent_message = await bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                if should_pin:
                    try:
                        await sent_message.pin()
                    except Exception as pin_err:
                        logging.warning(f"Could not pin custom Solana message: {pin_err}")
            except Exception as e:
                logging.error(f"Failed to send custom Solana message to {chat_id} with HTML: {e}")
                try:
                    plain_text = re.sub(r'<[^>]+>', '', reply_text)
                    await bot.send_message(chat_id=chat_id, text=plain_text)
                except Exception as fallback_err:
                    logging.error(f"Failed to send fallback custom Solana message to {chat_id}: {fallback_err}")
    except Exception as e:
        import traceback
        logging.error(f"Error in process_custom_address_check: {e}\n{traceback.format_exc()}")

async def handle_custom_api_check(request: web.Request) -> web.Response:
    """HTTP endpoint to manually trigger a wallet check from external services."""
    try:
        # Support both JSON POST payloads and query parameters (for GET requests)
        if request.method == "POST":
            data = await request.json()
        else:
            data = request.query
            
        address = data.get("address") or data.get("wallet")
        chat_id_raw = data.get("chat_id")
        
        if not address or not chat_id_raw:
            return web.json_response({
                "status": "error",
                "message": "Missing 'address' or 'chat_id' parameters."
            }, status=400)
            
        chat_id = int(chat_id_raw)
        
        # Process the balance check asynchronously in the background
        # to respond to the HTTP request immediately
        asyncio.create_task(process_custom_address_check(address, chat_id))
        
        return web.json_response({"status": "ok", "message": "Check queued successfully."})
    except ValueError:
        return web.json_response({"status": "error", "message": "chat_id must be an integer."}, status=400)
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)

@dp.message(Command("start", "help"))
async def send_welcome(message: types.Message):
    """Welcome and Help command handler."""
    welcome_text = (
        "👋 <b>Привет! Я бот для проверки баланса криптокошельков, токенов и доменных имен.</b>\n\n"
        "Добавьте меня в чат, и я буду автоматически отслеживать сообщения "
        "с адресами кошельков (EVM / Solana) или доменными именами (<b>.eth / .sol</b>).\n\n"
        "⭐ <b>Мои функции:</b>\n"
        "• Проверяет балансы нативных монет во всех популярных сетях.\n"
        "• 🪙 <b>Сканирует все ERC-20 токены</b> в сетях EVM и суммирует их стоимость.\n"
        "• Рассчитывает общую стоимость кошелька в USD по текущему курсу.\n"
        "• 📌 <b>Если баланс Solana кошелька > $11</b>, бот пришлет ссылку на <b>Solscan</b> и закрепит сообщение в чате.\n"
        "• 📌 <b>Если баланс EVM кошелька > $100</b>, бот пришлет ссылку на <b>Etherscan</b> и закрепит сообщение в чате.\n\n"
        "⚠️ <b>Важно для работы в группах:</b>\n"
        "Чтобы я видел сообщения в группе и мог закреплять их, вам нужно:\n"
        "1. Отключить <b>Privacy Mode</b> в BotFather (<code>/setprivacy</code> -> выбрать этого бота -> <code>Disable</code>).\n"
        "2. Выдать боту права <b>Администратора</b> (с разрешением <i>Закреплять сообщения</i>)."
    )
    await message.reply(welcome_text, parse_mode=ParseMode.HTML)

@dp.message(F.text | F.caption)
async def handle_address_detection(message: types.Message):
    """Detect, resolve domains, process wallet balances, calculate USD values, fetch ERC-20 tokens, and pin high-value balances."""
    try:
        text = message.text or message.caption
        if not text:
            return
            
        # Extract addresses and domains
        evm_addrs, sol_addrs, ens_domains, sns_domains = extract_addresses_and_domains(text)
        
        if not evm_addrs and not sol_addrs and not ens_domains and not sns_domains:
            return
            
        # Trigger typing state to show the bot is working
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        # Fetch price ticker
        prices = await get_token_prices()
        
        resolved_evm = {}  # domain -> resolved address
        resolved_sol = {}  # domain -> resolved address
        
        # Resolve domains concurrently
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Resolve ENS
            if ens_domains:
                ens_tasks = {domain: resolve_ens(client, domain) for domain in ens_domains}
                ens_results = await asyncio.gather(*ens_tasks.values())
                for domain, addr in zip(ens_tasks.keys(), ens_results):
                    if addr:
                        resolved_evm[domain] = addr
                        
            # Resolve SNS
            if sns_domains:
                sns_tasks = {domain: resolve_sns(client, domain) for domain in sns_domains}
                sns_results = await asyncio.gather(*sns_tasks.values())
                for domain, addr in zip(sns_tasks.keys(), sns_results):
                    if addr:
                        resolved_sol[domain] = addr
                        
        # Map addresses to display titles (e.g. "vitalik.eth (0xd8dA6...)")
        address_display_names = {}
        
        # Merge EVM addresses
        evm_to_check = list(evm_addrs)
        for domain, addr in resolved_evm.items():
            address_display_names[addr] = f"🔮 <b>{html.escape(domain)}</b>\n(<code>{html.escape(addr)}</code>)"
            if addr not in evm_to_check:
                evm_to_check.append(addr)
                
        for addr in evm_addrs:
            if addr not in address_display_names:
                address_display_names[addr] = f"Адрес: <code>{html.escape(addr)}</code>"
                
        # Merge Solana addresses
        sol_to_check = list(sol_addrs)
        for domain, addr in resolved_sol.items():
            address_display_names[addr] = f"🔮 <b>{html.escape(domain)}</b>\n(<code>{html.escape(addr)}</code>)"
            if addr not in sol_to_check:
                sol_to_check.append(addr)
                
        for addr in sol_addrs:
            if addr not in address_display_names:
                address_display_names[addr] = f"Адрес: <code>{html.escape(addr)}</code>"
                
        # If no valid addresses to check, exit
        if not evm_to_check and not sol_to_check:
            return
            
        # Process detected EVM addresses
        for addr in evm_to_check:
            # Fetch native balances and token balances concurrently
            async with httpx.AsyncClient(follow_redirects=True) as client:
                native_task = check_balances(addr, is_evm=True)
                
                token_tasks = [
                    fetch_blockscout_tokens(client, chain, base_url, addr)
                    for chain, base_url in BLOCKSCOUT_CONFIG.items()
                ]
                
                # Execute all checks in parallel
                native_balances, *tokens_results = await asyncio.gather(native_task, *token_tasks)
                
            # 1. Format Native Balances
            native_lines = []
            total_usd = 0.0
            
            for res in native_balances:
                if res["success"]:
                    val = res["balance"]
                    val_str = format_balance(val)
                    token = res["token"]
                    price = prices.get(token, 0.0)
                    usd_value = val * price
                    total_usd += usd_value
                    
                    usd_str = f" (~${usd_value:,.2f})" if usd_value > 0.01 else ""
                    native_lines.append(f"• <b>{html.escape(res['chain'])}</b>: <code>{val_str} {html.escape(token)}</code>{usd_str}")
                else:
                    native_lines.append(f"• <b>{html.escape(res['chain'])}</b>: <i>Ошибка RPC</i>")
                    
            # 2. Format ERC-20 Token Balances
            all_tokens = []
            for list_of_tokens in tokens_results:
                all_tokens.extend(list_of_tokens)
                
            # Sum up ERC-20 values
            for t in all_tokens:
                total_usd += t["usd_value"]
                
            # Sort tokens by value descending
            all_tokens = sorted(all_tokens, key=lambda x: x["usd_value"], reverse=True)
            
            token_lines = []
            for t in all_tokens[:8]:  # Show top 8 tokens
                val_str = format_balance(t["balance"])
                token_lines.append(f"• <b>{html.escape(t['symbol'])}</b> ({html.escape(t['chain'])}): <code>{val_str} {html.escape(t['symbol'])}</code> (~${t['usd_value']:,.2f})")
                
            if len(all_tokens) > 8:
                token_lines.append(f"<i>...и еще {len(all_tokens) - 8} токенов на общую сумму ${sum(t['usd_value'] for t in all_tokens[8:]):,.2f} USD</i>")
                
            balance_summary = "\n".join(native_lines)
            token_summary = "\n".join(token_lines)
            
            title = address_display_names.get(addr, addr)
            
            # Build reply text
            reply_text = (
                f"🔍 <b>EVM Wallet Summary</b>\n"
                f"{title}\n\n"
                f"💰 <b>Нативные балансы:</b>\n"
                f"{balance_summary}\n\n"
            )
            
            if token_lines:
                reply_text += (
                    f"🪙 <b>Токены (Топ-8 по ценности):</b>\n"
                    f"{token_summary}\n\n"
                )
                
            reply_text += f"💵 <b>Общая стоимость:</b> <b>${total_usd:,.2f} USD</b>"
            
            # Add Etherscan link if balance > $100
            should_pin = total_usd > 100.0
            if should_pin:
                reply_text += f"\n\n🔗 <a href=\"https://etherscan.io/address/{addr}\">Смотреть на Etherscan</a>"
                
            try:
                reply_message = await message.reply(reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                if should_pin:
                    try:
                        await reply_message.pin()
                    except Exception as pin_err:
                        logging.warning(f"Could not pin EVM message: {pin_err}")
            except Exception as e:
                logging.error(f"Failed to send EVM reply for {addr} with HTML: {e}")
                try:
                    plain_text = re.sub(r'<[^>]+>', '', reply_text)
                    await message.reply(plain_text)
                except Exception as fallback_err:
                    logging.error(f"Failed to send fallback EVM reply for {addr}: {fallback_err}")
            
        # Process detected Solana addresses
        for addr in sol_to_check:
            balances = await check_balances(addr, is_evm=False)
            lines = []
            total_usd = 0.0
            
            for res in balances:
                if res["success"]:
                    val = res["balance"]
                    val_str = format_balance(val)
                    token = res["token"]
                    price = prices.get(token, 0.0)
                    usd_value = val * price
                    total_usd += usd_value
                    
                    usd_str = f" (~${usd_value:,.2f})" if usd_value > 0.01 else ""
                    lines.append(f"• <b>{html.escape(res['chain'])}</b>: <code>{val_str} {html.escape(token)}</code>{usd_str}")
                else:
                    lines.append(f"• <b>{html.escape(res['chain'])}</b>: <i>Ошибка RPC</i>")
                    
            balance_summary = "\n".join(lines)
            title = address_display_names.get(addr, addr)
            
            # Build reply text
            reply_text = (
                f"🔍 <b>Solana Wallet Summary</b>\n"
                f"{title}\n\n"
                f"💰 <b>Баланс:</b>\n"
                f"{balance_summary}\n\n"
                f"💵 <b>Общая стоимость:</b> <b>${total_usd:,.2f} USD</b>"
            )
            
            # Add Solscan link if balance > $11
            should_pin = total_usd > 11.0
            if should_pin:
                reply_text += f"\n\n🔗 <a href=\"https://solscan.io/account/{addr}\">Смотреть на Solscan</a>"
                
            try:
                reply_message = await message.reply(reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                if should_pin:
                    try:
                        await reply_message.pin()
                    except Exception as pin_err:
                        logging.warning(f"Could not pin Solana message: {pin_err}")
            except Exception as e:
                logging.error(f"Failed to send Solana reply for {addr} with HTML: {e}")
                try:
                    plain_text = re.sub(r'<[^>]+>', '', reply_text)
                    await message.reply(plain_text)
                except Exception as fallback_err:
                    logging.error(f"Failed to send fallback Solana reply for {addr}: {fallback_err}")
    except Exception as e:
        import traceback
        logging.error(f"Unhandled error in handle_address_detection: {e}\n{traceback.format_exc()}")

async def on_startup(bot: Bot) -> None:
    """Set webhook when running on Render."""
    webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/webhook"
    logging.info(f"Setting webhook to: {webhook_url}")
    await bot.set_webhook(webhook_url)

async def main_polling():
    """Start bot in polling mode (default local)."""
    logging.info("Starting bot polling...")
    await dp.start_polling(bot)

def main():
    if not TOKEN:
        logging.critical("No token provided. Exiting.")
        return
        
    # Check if running in Webhook mode (Render environment provides PORT and RENDER_EXTERNAL_URL)
    port = os.getenv("PORT")
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    
    if port and external_url:
        logging.info(f"Running in Webhook mode on port {port}")
        # Webhook mode setup
        app = web.Application()
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=dp,
            bot=bot
        )
        webhook_requests_handler.register(app, path="/webhook")
        
        # Register our custom API route for other bots to hit
        app.router.add_route("*", "/api/check", handle_custom_api_check)
        
        dp.startup.register(on_startup)
        
        # Setup application
        setup_application(app, dp, bot=bot)
        
        # Run aiohttp server
        web.run_app(app, host="0.0.0.0", port=int(port))
    else:
        # Polling mode (local)
        try:
            asyncio.run(main_polling())
        except (KeyboardInterrupt, SystemExit):
            logging.info("Bot stopped.")

if __name__ == "__main__":
    main()
