# core/precision.py

from decimal import Decimal
import requests
import time
import hmac
import hashlib
from typing import Dict, Any

class PrecisionManager:
    """
    统一管理 OKX / Binance / Gate.io 的合约精度规则，
    并提供向下截断方法。
    """
    def __init__(self):
        # 存放规则：{ "okx": {...}, "binance": {...}, "gate": {...} }
        self.rules: Dict[str, Dict[str, Dict[str, str]]] = {
            "okx":     {},
            "binance": {},
            "gate":    {}
        }

    def load_okx(self, base_url: str):
        url  = f"{base_url}/api/v5/public/instruments"
        resp = requests.get(url, params={"instType":"SWAP"}, timeout=5).json()
        for itm in resp.get("data", []):
            self.rules["okx"][itm["instId"]] = {
                "stepSize": itm["lotSz"],
                "tickSize": itm["tickSz"]
            }

    def load_binance(self, base_url: str, api_key: str):
        url  = f"{base_url}/fapi/v1/exchangeInfo"
        headers = {"X-MBX-APIKEY": api_key}
        resp = requests.get(url, headers=headers, timeout=5).json()
        for s in resp.get("symbols", []):
            fs = {f["filterType"]: f for f in s["filters"]}
            self.rules["binance"][s["symbol"]] = {
                "stepSize": fs["LOT_SIZE"]["stepSize"],
                "tickSize": fs["PRICE_FILTER"]["tickSize"]
            }

    def load_gate(self, base_url: str):
        """
        从 Gate 的 futures/contracts 接口加载合约精度，
        只处理 dict 类型的条目，跳过字符串。
        """
        url  = f"{base_url}/api/v4/futures/contracts"
        resp = requests.get(url, timeout=5).json()

        for itm in resp:
            # 如果不是字典，跳过
            if not isinstance(itm, dict):
                continue

            pair = itm.get("currency_pair")
            size_inc  = itm.get("sizeIncrement")
            price_inc = itm.get("priceIncrement")

            # 额外保险：确保这三个字段都存在
            if not pair or size_inc is None or price_inc is None:
                continue

            self.rules["gate"][pair] = {
                "stepSize": size_inc,
                "tickSize": price_inc
            }
    @staticmethod
    def truncate_down(value: float, step: str) -> float:
        """
        向下截断到最接近 step 的整数倍。
        """
        d = Decimal(str(value))
        s = Decimal(step)
        return float((d // s) * s)

    def get_truncated(
        self,
        exchange: str,
        symbol: str,
        raw_qty: float,
        raw_price: float = None
    ) -> (float, float):
        """
        根据 exchange 和 symbol 拿到 stepSize/tickSize，
        返回安全 (qty, price)。
        """
        rule = self.rules.get(exchange, {}).get(symbol, {})
        step = rule.get("stepSize", "1")
        tick = rule.get("tickSize", "0.01")
        safe_qty   = self.truncate_down(raw_qty,   step)
        safe_price = None if raw_price is None else self.truncate_down(raw_price, tick)
        return safe_qty, safe_price
