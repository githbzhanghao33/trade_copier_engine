# core/api_gate.py
from utils.symbol_utils import convert_symbol as _convert
from typing import Dict, Any, List, Tuple
import time
import uuid
import hmac
import hashlib
import requests
import logging
import json
from requests.exceptions import HTTPError

class GateAPI:
    """
    Gate.io 永续合约 API Wrapper
    私有接口（下单、持仓、资金）都需签名；公共行情接口无需签名。
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str = "",           # Gate.io 不用，但为统一接口保留
        base_url: str = "https://api.gateio.ws",
        logger: logging.Logger = None,
    ):
        # —— 客户端 & 引擎 都需要这几个标准属性 —— 
        self.api_key     = api_key
        self.api_secret  = api_secret
        self.passphrase  = passphrase
        self.name        = "gate"

        # 基础 URL
        self.base_url    = base_url.rstrip("/")
        self._base_url   = self.base_url

        # 日志
        self.logger      = logger or logging.getLogger("GateAPI")


    def _sign(
        self,
        method: str,
        path: str,
        query: str = "",
        body_dict: Dict[str, Any] | None = None
    ) -> Dict[str, str]:
        """
        Gate API v4 私有接口签名（HMAC-SHA512）：
        签名原文 = METHOD + "\n" +
                 PATH + "\n" +
                 QUERY + "\n" +
                 SHA512_HEX(BODY) + "\n" +
                 TIMESTAMP(秒级)
        """
        ts = str(int(self.get_server_time() / 1000))  # 秒级时间戳

        # 计算 body 的 SHA-512 hex 摘要
        if body_dict is None:
            body_hash = hashlib.sha512(b"").hexdigest()
        else:
            body_str  = json.dumps(body_dict, separators=(",", ":"))
            body_hash = hashlib.sha512(body_str.encode()).hexdigest()

        # 拼签名原文
        sign_str = "\n".join([
            method.upper(),
            path,
            query,
            body_hash,
            ts
        ])

        signature = hmac.new(
            self.api_secret.encode(),
            sign_str.encode(),
            hashlib.sha512
        ).hexdigest()

        return {
            "KEY":         self.api_key,
            "Timestamp":   ts,
            "SIGN":        signature,
            "Content-Type":"application/json"
        }

    def test_connection(self) -> bool:
        """
        上传配置时调用：用 /futures/positions 测试 Key/Secret 是否有效。
        """
        path    = "/api/v4/futures/positions"
        url     = f"{self._base_url}{path}"
        headers = self._sign("GET", path)

        resp = requests.get(url, headers=headers, timeout=5)
        try:
            data = resp.json()
        except ValueError:
            raise Exception(f"Gate test_connection: 非 JSON 响应，状态码 {resp.status_code}")

        # 如果有 error 字段，认为鉴权失败
        if isinstance(data, dict) and data.get("error"):
            raise Exception(f"Gate 鉴权失败：{data['error']}")

        return True


    def convert_symbol(self, symbol: str) -> str:
        return _convert(symbol, to_ex="gate")

    def get_positions(self) -> Dict[str, float]:
        """
        拉取所有 USDT 永续合约持仓：
        GET /api/v4/futures/usdt/positions
        返回 { contract: net_size, … }
        """
        path    = "/api/v4/futures/usdt/positions"
        url     = f"{self._base_url}{path}"
    # 签名中使用服务器时间戳（通过 get_server_time）
        headers = self._sign("GET", path)

        resp = requests.get(url, headers=headers, timeout=5)
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            self.logger.error(f"[Gate:get_positions] HTTP {resp.status_code} 错误，返回：{resp.text}")
        return {}
        data = resp.json()

    # 确保返回列表
        if not isinstance(data, list):
            if isinstance(data, dict) and data.get("error"):
                self.logger.error(f"[Gate:get_positions] error: {data['error']}")
            else:
                self.logger.error(f"[Gate:get_positions] 返回格式非法: {data}")
            return {}

        result: Dict[str, float] = {}
        for itm in data:
            if not isinstance(itm, dict):
                continue
            contract = itm.get("contract")
            size     = float(itm.get("size", 0))
            direction= itm.get("direction", "").lower()
            net      = size if direction == "long" else -size
            if net != 0:
                result[contract] = net
        return result

    def get_position_qty(self, symbol: str) -> float:
        """
        查询单个合约净持仓：
          GET /futures/usdt/positions?contract=XXX
          返回 size * (+1 或 -1)
        """
        tgt  = self.convert_symbol(symbol)
        path = f"/api/v4/futures/usdt/positions?contract={tgt}"
        url  = f"{self._base_url}{path}"
        headers = self._sign("GET", path)

        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()

        if isinstance(data, dict) and data.get("error"):
            raise Exception(f"Gate get_position_qty error: {data['error']}")

        # 返回列表，取第一个
        if isinstance(data, list) and data:
            itm = data[0]
            size = float(itm.get("size", 0))
            direction = itm.get("direction", "").lower()
            return size if direction == "long" else -size

        return 0.0


    def get_balance(self) -> float:
        """
        查询 USDT 可用余额：
          GET /futures/usdt/accounts
        """
        path = "/api/v4/futures/usdt/accounts"
        url  = f"{self._base_url}{path}"
        headers = self._sign("GET", path)

        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()

        if isinstance(data, dict) and data.get("error"):
            raise Exception(f"Gate get_balance error: {data['error']}")

        # data 应为列表，按 currency 查 USDT
        for itm in data or []:
            if itm.get("currency") == "USDT":
                return float(itm.get("available", 0.0))
        return 0.0


    def get_best_price(self, symbol: str) -> Tuple[float, float]:
        """
        获取顶层买一/卖一：
          GET /futures/order_book?contract=XXX&limit=1
        """
        tgt  = self.convert_symbol(symbol)
        self.logger.debug(f"[Gate] convert_symbol: 入参 symbol={symbol} → contract={tgt}")
        path = f"/api/v4/futures/usdt/tickers"
        url  = f"{self._base_url}{path}"
        params = {"contract": tgt}
        resp = requests.get(url, params, timeout=5)
        self.logger.debug(f"[Gate] HTTP {resp.status_code} {resp.url}")
        self.logger.debug(f"[Gate] 原始返回：{resp.text}")
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            lst = data
        else:
            lst = data.get("result", [])

        if not lst:
            return 0.0, 0.0

        ticker   = lst[0]
        best_bid = float(ticker.get("highest_bid", 0) or ticker.get("best_bid", 0))
        best_ask = float(ticker.get("lowest_ask",  0) or ticker.get("best_ask", 0))
        return best_bid, best_ask

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float = None,
        reduce_only: bool = False
    ) -> Dict[str, Any]:
        """
        永续合约下单（限价 or 市价）：
        POST /futures/orders
        """
        tgt  = self.convert_symbol(symbol)
        path = "/api/v4/futures/usdt/orders"
        url  = f"{self._base_url}{path}"

    # 构造请求体
    # -- text 必须以 t- 开头，后面最好带个唯一标识 --
        unique_id = uuid.uuid4().hex[:8]
        text      = f"t-{int(time.time())}-{unique_id}"

        body: Dict[str, Any] = {
            "contract":   tgt,
            "size":       quantity,
            "side":       side.upper(),
            "reduceOnly": reduce_only,
            "text":       text,
            "tif":        "ioc" if price is None else "gtc"
        }
        if price is not None:
            body["price"] = price

    # 签名和发送逻辑保持之前正确的方式
        body_str = json.dumps(body, separators=(",", ":"))
        headers  = self._sign("POST", path, query="", body_dict=body)
        resp     = requests.post(url, headers=headers, data=body_str, timeout=5)
        try:
            resp.raise_for_status()
        except HTTPError as err:
            self.logger.error(f"[Gate] 下单失败 HTTP 错误: {err}")
            self.logger.error(f"[Gate] 错误详情: {resp.text}")
            raise Exception(f"HTTP Error: {err}, Response: {resp.text}")

        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise Exception(f"Gate place_order error: {data['error']}")

        return data
    def fetch_new_orders(self, since_ts: float = None) -> List[Dict[str, Any]]:
        """
        拉取成交填单（Fills）：
          GET /futures/usdt/trades?contract=XXX&from=timestamp
        """
        # 示例用 symbol 'SOL-USDT-SWAP'，实际调用时请传入正确 symbol
        # 这里仅演示接口用法，建议在 engine 层过滤符号
        path = "/futures/usdt/trades"
        params = {}
        # contract 参数
        # contract = self.convert_symbol(symbol)
        # params["contract"] = contract
        if since_ts:
            params["from"] = int(since_ts / 1000)

        # 构造完整 path + query
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_path = f"{path}?{query}" if query else path
        url = f"{self._base_url}{full_path}"
        headers = self._sign("GET", full_path)

        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise Exception(f"Gate fetch_new_orders error: {data['error']}")

        orders: List[Dict[str, Any]] = []
        for it in data or []:
            orders.append({
                "timestamp": int(it["create_time_ms"]),
                "symbol":    it["contract"],
                "side":      it["direction"].lower(),
                "qty":       float(it["size"]),
                "price":     float(it["price"]),
                "order_id":  it["id"]
            })
        return orders

    def set_leverage(
        self,
        symbol: str,
        leverage: int,
        settle: str = "usdt"
    ) -> Dict[str, Any]:
        """
        设置 USDT 永续合约杠杆倍数（签名请求）。
        POST /api/v4/futures/{settle}/positions/{contract}/leverage?leverage={leverage}
        """
        # 1) 拼接路径与查询串
        contract = self.convert_symbol(symbol)
        path     = f"/api/v4/futures/{settle}/positions/{contract}/leverage"
        query    = f"leverage={leverage}"
        url      = f"{self._base_url}{path}?{query}"

        # 2) 生成签名头（不再带 body）
        headers = self._sign("POST", path, query=query, body_dict=None)

        try:
            # 3) 发请求
            resp = requests.post(url, headers=headers, timeout=5)
            resp.raise_for_status()

            # 4) 直接返回完整信息，Gate 返回的数据里没有 "result" 字段
            data = resp.json()
            self.logger.info(f"[Gate] 杠杆已设置: {contract} → {leverage}x")
            return data

        except HTTPError as err:
            # 打印 HTTP 错误码和详情
            self.logger.error(f"[Gate] 设置杠杆失败 HTTP 错误: {err}")
            self.logger.error(f"[Gate] 错误详情: {resp.text}")
            raise Exception(f"HTTP Error: {err}, Response: {resp.text}")

        except Exception as e:
            self.logger.error(f"[Gate] 设置杠杆请求出错: {e}")
            raise
    def get_server_time(self) -> int:
        """
        获取 Gate.io 服务端当前时间（毫秒级），无需身份认证。
        GET /api/v4/spot/time
        """
        path = "/api/v4/spot/time"
        url  = f"{self._base_url}{path}"
        headers = {"Accept": "application/json"}

        try:
            resp = requests.get(url, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return int(data.get("server_time", 0))
        except Exception as e:
            self.logger.error(f"[Gate:get_server_time] 请求失败: {e}")
            return 0
