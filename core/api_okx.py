# core/api_okx.py
# -*- coding: utf-8 -*-
from utils.symbol_utils import convert_symbol as _convert
from decimal import Decimal
from typing import Dict, Any
import logging
import json
import hmac
import hashlib
import base64
import requests
from datetime import datetime

class OkxAPI:
    """OKX 交易所 API 封装"""
    EXCHANGE_NAME = "OKX"
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str = "",
        base_url: str = "https://www.okx.com",
        logger: logging.Logger = None,
    ):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._base_url  = base_url.rstrip("/")

        # 如果外部没传 logger，就用模块级 logger
        self.logger = logger or logging.getLogger(f"OkxAPI[{self.api_key[-4:]}]")
        # ── 动态拉取所有 SWAP 合约的面值(ctVal) 并缓存 ──
        data = requests.get(
            f"{self._base_url}/api/v5/public/instruments",
            params={"instType": "SWAP"},
            timeout=5
        ).json().get("data", [])

        # ─── 2. 构建 inst_map：instId -> ctVal（合约面值：币/张） ─────
        self.inst_map: Dict[str, float] = {
            itm["instId"]: float(itm["ctVal"])
            for itm in data
            if itm.get("instId") and itm.get("ctVal")
        }

        # ─── 3. 构建 rules：instId -> { minSz, lotSz, tickSz } ───────
        self.rules: Dict[str, Dict[str, Decimal]] = {}
        for itm in data:
            sym = itm["instId"]
            # Decimal 方便后续精确计算
            self.rules[sym] = {
                "minSz":  Decimal(itm["minSz"]),   # 最小下单币数
                "lotSz":  Decimal(itm["lotSz"]),   # 最小下单合约张数
                "tickSz": Decimal(itm["tickSz"])   # 最小价格步长
            }
        for sym, ctval in self.inst_map.items():
            if sym in self.rules:
                self.rules[sym]["ctVal"] = Decimal(str(ctval))
        # ─── 4. supported_symbols：所有支持的 instId 列表 ──────────────
        self.supported_symbols = set(self.inst_map.keys())

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        prehash = timestamp + method + request_path + body
        h = hmac.new(
            self.api_secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(h.digest()).decode()

    @staticmethod
    def _get_okx_timestamp() -> str:
        """
        从 OKX 公共接口获取服务器时间，避免本地时钟漂移。
        OKX v5 /public/time 返回 data 是一个列表：[{"ts": "..."}]
        """
        r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
        r.raise_for_status()
        raw = r.json()
        # data 应该是列表
        data_list = raw.get("data")
        if not isinstance(data_list, list) or len(data_list) == 0:
            raise Exception(f"Unexpected time response format: {raw}")
        first = data_list[0]
        ts_str = first.get("ts")
        if ts_str is None:
            raise Exception(f"Missing 'ts' in time response: {first}")
        # ts_str 是毫秒级字符串
        ts = int(ts_str)
        dt = datetime.utcfromtimestamp(ts / 1000.0)
        return dt.isoformat("T", "milliseconds") + "Z"

    @staticmethod
    def test_connection() -> bool:
        try:
            r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
            return r.status_code == 200
        except:
            return False

    def supports_symbol(self, symbol: str) -> bool:
        norm = symbol.upper().replace("_", "-")
        if not norm.endswith("-SWAP"):
            norm += "-SWAP"
        return norm in self.supported_symbols

    def convert_symbol(self, symbol: str) -> str:
        return _convert(symbol, to_ex=self.EXCHANGE_NAME)

    def fetch_new_orders(self, since_ts: int = None):
        request_path = "/api/v5/trade/fills?instType=SWAP"
        if since_ts:
            request_path += f"&after={int(since_ts)}"
        url = "https://www.okx.com" + request_path
        method = "GET"
        body = ""

        timestamp = self._get_okx_timestamp()
        sign = self._sign(timestamp, method, request_path, body)
        headers = {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       sign,
            "OK-ACCESS-TIMESTAMP":  timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json"
        }

        print(f"[DEBUG] 请求 URL: {url}")
        print(f"[DEBUG] 请求 Headers: {headers}")

        resp = requests.get(url, headers=headers, timeout=5)

        try:
            data = resp.json()
        except Exception as e:
            print(f"[ERROR] JSON 解析失败: {e}")
            raise

        print(f"[DEBUG] 返回数据: {data}")

        if not isinstance(data, dict):
            raise Exception(f"返回格式异常，预期为 dict，实际为: {type(data)}")

        if data.get("code") != "0":
            err = data.get("msg", data)
            raise Exception(f"OKX fetch_new_orders error: {err}")

        orders = []
        for item in data.get("data", []):
            order = {
                "timestamp": int(item.get("fillTime", 0)),
                "symbol":    item.get("instId"),
                "side":      item.get("side", "").lower(),
                "qty":       float(item.get("fillSz", 0)),
                "price":     float(item.get("fillPx", 0)),
                "order_id":  item.get("tradeId")
            }
            orders.append(order)
        return orders

    def get_best_price(self, symbol: str) -> tuple:
        url = f"https://www.okx.com/api/v5/market/ticker?instId={symbol}"
        resp = requests.get(url, timeout=5)
        print(f"[get_best_price] status={resp.status_code}, url={url}")
        data = resp.json()
        print(f"[get_best_price] response: {data}")
        return float(data["data"][0]["bidPx"]), float(data["data"][0]["askPx"])

    def get_position_qty(self, symbol: str) -> float:
        """
        查询 OKX 永续合约净持仓，并把“合约张数”乘以合约面值(ctVal) 
        转换成“标的币数量”返回。
        """
        # 1. 调接口
        path = f"/api/v5/account/positions?instId={symbol}"
        url  = f"{self._base_url}{path}"
        ts   = self._get_okx_timestamp()
        sign = self._sign(ts, "GET", path, "")
        headers = {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       sign,
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        if data.get("code") != "0":
            raise Exception(f"OKX get_position_qty error: {data.get('msg', data)}")

        # 2. 根据 inst_map 把合约张数转换为标的币数量
        #    inst_map: { "BTC-USDT-SWAP": 0.001, "DOGE-USDT-SWAP": 0.1, ... }
        ct_val = self.inst_map.get(symbol, 1.0)

        net_qty = 0.0
        for p in data.get("data", []):
            # 原始合约张数
            try:
                contracts = float(p.get("pos") or "0")
            except ValueError:
                contracts = 0.0

            # 换算成标的币数量
            qty = contracts * ct_val

            # 净多/净空累加
            side_flag = p.get("posSide", "long").lower()
            if side_flag == "long":
                net_qty += qty
            else:
                net_qty -= qty

        if self.logger:
            self.logger.debug(f"[OKX] get_position_qty({symbol}) → {net_qty}")
        return net_qty

    def set_leverage(self, symbol: str, leverage: int):
        request_path = "/api/v5/account/set-leverage"
        url = "https://www.okx.com" + request_path
        body = json.dumps({"instId": symbol, "lever": str(leverage), "mgnMode": "cross"})

        timestamp = self._get_okx_timestamp()
        sign = self._sign(timestamp, "POST", request_path, body)
        headers = {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       sign,
            "OK-ACCESS-TIMESTAMP":  timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json"
        }
        print(f"[set_leverage] url={url}, body={body}, headers={headers}")
        resp = requests.post(url, headers=headers, data=body, timeout=5)
        data = resp.json()
        print(f"[set_leverage] response: {data}")
        if data.get("code") != "0":
            err = data.get("msg", data)
            raise Exception(f"OKX set_leverage error: {err}")

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float = None,
        order_type: str = None
    ) -> Dict[str, Any]:
        """
        下单（币数量 → 合约张数转换，校验 minSz & lotSz）：
          - quantity: 标的币数量（如 39.45 DOGE）
          - price:    限价单价格，None 表示市价
          - order_type: 可选 "LIMIT"/"MARKET"，默认为自动选
        """
        # 1) 验证合约是否存在
        symbol = self.convert_symbol(symbol)
        rule = self.rules.get(symbol)
        if not rule:
            raise ValueError(f"Unknown symbol for OKX: {symbol}")

        ctVal  = rule["ctVal"]
        minSz  = rule["minSz"]
        lotSz  = rule["lotSz"]
        tickSz = rule["tickSz"]

        raw_qty = Decimal(str(quantity))

        # 2) 校验最小币数
        if raw_qty < minSz:
            self.logger.warning(
                f"[OKX] {symbol} quantity {raw_qty} < minSz {minSz}，跳过下单"
            )
            return {}

        # 3) 币数 → 合约张数
        contracts = raw_qty / ctVal

        # 4) 向下截断到最小合约张数 lotSz
        contracts = (contracts // lotSz) * lotSz
        if contracts <= 0:
            self.logger.warning(
                f"[OKX] {symbol} 币数→张数后 contracts={contracts} ≤ 0，跳过下单"
            )
            return {}

        # 5) 构造下单参数
        ord_type = order_type or ("limit" if price is not None else "market")
        payload = {
            "instId":  symbol,
            "tdMode":  "cross",
            "side":    side.lower(),
            "posSide": "long" if side.lower() == "buy" else "short",
            "ordType": ord_type,
            "sz":      str(contracts),  # 合约张数
        }

        # 6) 价格截断到 tickSz
        if price is not None:
            p = (Decimal(str(price)) // tickSz) * tickSz
            payload["px"] = str(p)

        self.logger.info(f"[OKX 下单参数] ordType={ord_type}, px={payload.get('px')}, payload={payload}")
        body = json.dumps(payload)
        ts   = self._get_okx_timestamp()
        sign = self._sign(ts, "POST", "/api/v5/trade/order", body)

        headers = {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       sign,
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json"
        }

        # 7) 发送请求
        resp = requests.post(
            f"{self._base_url}/api/v5/trade/order",
            headers=headers,
            data=body,
            timeout=5
        )
        data = resp.json()
        if data.get("code") != "0":
            raise Exception(f"OKX place_order error: {data}")

        self.logger.info(
            f"[OKX] 下单成功: {symbol} {side} "
            f"{contracts}张 @ {payload.get('px', 'MARKET')}"
        )
        return data

    def get_positions(self) -> Dict[str, float]:
        """
        拉取 OKX 所有账户持仓（现货/交割/永续混合），
        并把“合约张数”乘以 ctVal 转成“标的币数量”后返回：
        { instId: net_qty_in_coin, … }
        """
        path   = "/api/v5/account/positions"
        url    = self._base_url + path
        method = "GET"
        body   = ""

        # 签名头
        timestamp = self._get_okx_timestamp()
        sign      = self._sign(timestamp, method, path, body)
        headers = {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       sign,
            "OK-ACCESS-TIMESTAMP":  timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json"
        }

        # 请求
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()

        # 错误检查
        if data.get("code") != "0":
            self.logger.error(f"[OKX:get_positions] API error: {data}")
            return {}

        result: Dict[str, float] = {}
        for p in data.get("data", []):
            inst = p.get("instId") or ""
            # 原始合约张数（字符串转 float）
            raw = p.get("pos") or p.get("availPos") or "0"
            try:
                contracts = float(raw)
            except (TypeError, ValueError):
                contracts = 0.0

            # 如果是永续/交割合约，inst_map 中会有 ctVal，用来算币量
            # 否则（现货等场景）直接用 contracts 作为币量
            ct_val = self.inst_map.get(inst)
            if ct_val is not None:
                qty = contracts * ct_val
            else:
                qty = contracts

            # 如果是空头，取负值
            if p.get("posSide", "").lower() == "short":
                qty = -abs(qty)

            # 记录：instId -> 标的币净持仓
            result[inst] = qty

            # 可选：打印 debug
           # self.logger.debug(
           #     f"[OKX] get_positions {inst}: contracts={contracts}, "
           #     f"ctVal={ct_val}, qty_in_coin={qty}"
           # )

        return result

    def get_balance(self, ccy: str = "USDT") -> float:
        """
        调用 OKX /api/v5/account/balance 接口获取指定币种的可用余额
        """
    # 1. 拼 path（带参数）
        path = f"/api/v5/account/balance?ccy={ccy}"

    # 2. 拉服务器时间戳
        timestamp = self._get_okx_timestamp()

    # 3. 签名（body 为空字符串）
        sign = self._sign(timestamp, "GET", path, "")

    # 4. Headers
        headers = {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       sign,
            "OK-ACCESS-TIMESTAMP":  timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json"
        }

    # 5. 拼 URL
        url = self._base_url + path
        self.logger.info(f"[OKX:get_balance] GET {url}")
        self.logger.info(f"[OKX:get_balance] Headers: {headers}")

    # 6. 请求
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        self.logger.info(f"[OKX:get_balance] Response: {data}")
    # 7. 错误检查
        if data.get("code") != "0":
            self.logger.error(f"[OKX:get_balance] API error: {data}")
            return 0.0

    # 8. 下钻到 data[0]['details'] 解析可用余额
        for blk in data.get("data", []):
        # 有的账号直接返回 blk['details']，有的在 blk 本身
            details = blk.get("details") or [blk]
            for itm in details:
                if itm.get("ccy") == ccy:
                    bal = itm.get("availBal") or itm.get("bal") or "0"
                    try:
                        return float(bal)
                    except ValueError:
                        self.logger.error(f"[OKX:get_balance] API error: {data}")
                        return 0.0
                        self.logger.warning(f"[OKX:get_balance] 未找到余额字段, 原始返回: {data}")
    # 找不到就返回 0.0
        return 0.0
