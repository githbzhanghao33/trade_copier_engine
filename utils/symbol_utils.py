# utils/symbol_utils.py

def detect_exchange(symbol: str) -> str:
    """
    根据符号格式自动判断来源交易所：
      - 含 '-' 的当做 OKX（如 "BTC-USDT-SWAP"）
      - 含 '_' 的当做 Gate（如 "BTC_USDT"）
      - 其他当做 Binance（如 "BTCUSDT"）
    """
    if "-" in symbol:
        return "okx"
    if "_" in symbol:
        return "gate"
    return "binance"

def normalize_symbol(symbol: str, from_ex: str) -> str:
    s = symbol.upper()
    ex = from_ex.lower()
    if ex == "okx":
        s = s.replace("-SWAP", "").replace("-", "")
    elif ex == "gate":
        s = s.replace("_", "")
    elif ex == "binance":
        pass
    else:
        raise ValueError(f"Unsupported from_ex: {from_ex}")
    return s

def to_exchange_symbol(base: str, to_ex: str) -> str:
    ex = to_ex.lower()
    if ex == "okx":
        # 合约按 USDT 末尾拆分
        return f"{base[:-4]}-{base[-4:]}-SWAP"
    if ex == "gate":
        return f"{base[:-4]}_{base[-4:]}"
    if ex == "binance":
        return base
    raise ValueError(f"Unsupported to_ex: {to_ex}")

def convert_symbol(symbol: str, to_ex: str) -> str:
    """
    单参数转换：自动 detect from_ex，再转 to_ex
    """
    from_ex = detect_exchange(symbol)
    base    = normalize_symbol(symbol, from_ex)
    return to_exchange_symbol(base, to_ex)
