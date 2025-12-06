# core/api_binance.py
# -*- coding: utf-8 -*-
from utils.symbol_utils import convert_symbol as _convert
from typing import Dict
import logging
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from typing import Tuple, Dict, Any

class BinanceAPI:
    """Binance 交易所 USDT 合约 API 封装"""
    BASE_FUTURES = "https://fapi.binance.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://fapi.binance.com",
        logger: logging.Logger = None,
    ):
        self.api_key    = api_key
        self.api_secret = api_secret
        self._base_url  = base_url.rstrip("/")
        # 如果没传 logger，则 fallback 到模块级 logger
        name = f"BinanceAPI[{self.api_key[-4:]}]"
        self.logger = logger or logging.getLogger(name)

    def test_connection(self) -> bool:
        """
        上传配置时鉴权调用：
        尝试拉取期货账户信息，如果返回正常就通过
        """
        ts = self._get_server_time()
        params = {"timestamp": ts}
        qs = urlencode(params)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        headers = {"X-MBX-APIKEY": self.api_key}
        url = f"{self._base_url}/fapi/v2/account"
        resp = requests.get(url, headers=headers, params=params, timeout=5)

        try:
            data = resp.json()
        except ValueError:
            raise Exception(f"Binance 鉴权失败：非 JSON 响应，状态码 {resp.status_code}")

        if resp.status_code != 200 or data.get("code"):
            raise Exception(f"Binance 鉴权失败：{data.get('msg', data)}")
        return True
    def _get_server_time(self) -> int:
        """获取 Binance 服务器时间（毫秒），失败则用本地时间"""
        try:
            r = requests.get("https://api.binance.com/api/v3/time", timeout=5)
            return int(r.json().get("serverTime", time.time() * 1000))
        except Exception as e:
            if self.logger:
                self.logger.error(f"获取 Binance 服务器时间失败: {e}")
            return int(time.time() * 1000)

    def convert_symbol(self, symbol: str) -> str:
        return _convert(symbol, to_ex="binance")


    def get_balance(self) -> float:
        try:

            url = f"{self.BASE_FUTURES}/fapi/v2/balance"
            headers = {"X-MBX-APIKEY": self.api_key}

        # 用你自己的时间同步方法
            ts = self._get_server_time()   # 这里用你的封装
            params = {"timestamp": ts, "recvWindow": 5000}
            qs = urlencode(params)
            signature = hmac.new(
                self.api_secret.encode(),
                qs.encode(),
                hashlib.sha256
            ).hexdigest()
            params["signature"] = signature

            resp = requests.get(url, headers=headers, params=params, timeout=5)
            data = resp.json()
            for asset in data if isinstance(data, list) else []:
                if asset.get("asset") == "USDT":
                    bal_str = asset.get("balance") or "0"
                    try:
                        return float(bal_str)
                    except ValueError:
                        return 0.0
        except Exception as e:
            if self.logger:
                self.logger.error(f"跟binance {self.api_key[-4:]} 查询余额失败: {e}")
        return 0.0
    def get_position_qty(self, symbol: str) -> float:
        """
        查询该 symbol 在币安永续合约上的净持仓数（正多负空）。
        调用 /fapi/v2/positionRisk 接口，需要签名。
        """
        path = "/fapi/v2/positionRisk"
        ts   = self._get_server_time()
        params = {
            "symbol":     symbol,
            "timestamp":  ts,
            "recvWindow": 60000,
        }
        qs = urlencode(params)
        # 生成签名
        signature = hmac.new(
            self.api_secret.encode(),
            qs.encode(),
            hashlib.sha256
        ).hexdigest()
        url = f"https://fapi.binance.com{path}?{qs}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}

        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        if not isinstance(data, list):
            # 返回出错
            raise Exception(f"Binance get_position error: {data}")

        # 找到对应 symbol 的那一项
        for pos in data:
            if pos.get("symbol") == symbol:
                # positionAmt 字段可能是字符串，例如 "0.010"
                try:
                    amt = float(pos.get("positionAmt", "0"))
                except ValueError:
                    amt = 0.0
                return amt

        # 若列表里没有该 symbol，则等同于 0
        return 0.0
    def get_best_price(self, symbol: str) -> Tuple[float, float]:
        """获取买一价/卖一价"""
        sym = self.convert_symbol(symbol)
        url = f"{self.BASE_FUTURES}/fapi/v1/ticker/bookTicker?symbol={sym}"
        try:
            resp = requests.get(url, timeout=5)
            data = resp.json()
            bid = float(data.get("bidPrice") or 0.0)
            ask = float(data.get("askPrice") or 0.0)
            return bid, ask
        except Exception as e:
            if self.logger:
                self.logger.error(f"跟binance {self.api_key[-4:]} 获取最优价失败 {sym}: {e}")
            raise

    def set_leverage(self, symbol: str, leverage: int):
        """设置杠杆倍数（签名请求）"""
        sym = self.convert_symbol(symbol)
        url = f"{self.BASE_FUTURES}/fapi/v1/leverage"
        headers = {"X-MBX-APIKEY": self.api_key}
        params = {
            "symbol":    sym,
            "leverage":  leverage,
            "timestamp": self._get_server_time(),
            "recvWindow": 60000
        }
        qs = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()
        resp = requests.post(url, headers=headers, params=params, timeout=5)
        data = resp.json()
        if data.get("code") and data["code"] != 200:
            raise Exception(f"Binance set_leverage error: {data}")

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float = None,
        tp_ratio: float = 0,
        sl_ratio: float = 0
    ) -> Dict[str, Any]:
        """签名下单（LIMIT 或 MARKET）"""
        sym = self.convert_symbol(symbol)
        url = f"{self.BASE_FUTURES}/fapi/v1/order"
        headers = {"X-MBX-APIKEY": self.api_key}

        params = {
            "symbol":     sym,
            "side":       side.upper(),
            "type":       "LIMIT" if price else "MARKET",
            "quantity":   quantity,
            "timestamp":  self._get_server_time(),
            "recvWindow": 60000
        }
        if price:
            params["price"] = price
            params["timeInForce"] = "GTC"

        qs = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

        resp = requests.post(url, headers=headers, params=params, timeout=5)
        data = resp.json()
        if data.get("code") and data["code"] != 0:
            raise Exception(f"Binance place_order error: {data}")
        return data

    def get_positions(self) -> Dict[str, float]:
        """
        拉取 Binance 永续合约所有持仓，返回 {symbol: positionAmt, …}。
        调用 /fapi/v2/positionRisk，需要签名。
        """
        # 1. 构造签名
        ts = self._get_server_time()
        params = {
            "timestamp": ts,
            "recvWindow": 60000
        }
        qs = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

        # 2. 发起请求
        url = f"{self._base_url}/fapi/v2/positionRisk?{qs}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        resp    = requests.get(url, headers=headers, timeout=5)
        data    = resp.json()

        # 3. 解析并返回
        result: Dict[str, float] = {}
        if isinstance(data, list):
            for itm in data:
                sym = itm.get("symbol")
                try:
                    amt = float(itm.get("positionAmt", 0))
                except (TypeError, ValueError):
                    amt = 0.0
                result[sym] = amt
        else:
            self.logger.error(f"[Binance:get_positions] unexpected response: {data}")
        return result
