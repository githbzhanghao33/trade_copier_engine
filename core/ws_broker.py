# core/ws_broker.py

import json
import threading
import time
import requests
import hmac
import hashlib
import base64
import logging
from collections import defaultdict
from datetime import datetime
from websocket import WebSocketApp

logger = logging.getLogger(__name__)

class WSBroker:
    """
    为每个 group/target 启动 OKX/Binance WS，缓存最新持仓到
      self.positions_cache[group][suffix] = [ {...}, {...}, … ]
    """

    def __init__(self, group_engines):
        self.group_engines    = group_engines
        # 两级 dict：分组 → api_key 后缀 → 持仓列表
        self.positions_cache  = defaultdict(lambda: defaultdict(list))
        # 预加载 Binance 合约面值
        self._binance_map     = self._load_binance_inst_map()
        # 一次性为已有引擎订阅所有 WS
        self._start_all()

    def _start_all(self):
        """为当前所有分组和 targets 重新发起 WS 订阅"""
        for group, engine in self.group_engines.items():
            logger.debug(f"[WSBroker] start_all subscribing group={group}")
            for tgt in engine.targets:
                ex  = tgt.get("name", "").lower()
                api = tgt.get("api")
                if not api:
                    logger.warning(f"[WSBroker] target missing api instance: {tgt}")
                    continue
                key = getattr(api, 'api_key', None)
                sec = getattr(api, 'api_secret', '')
                suf = key[-4:] if key else ''
                if ex == "okx":
                    passphrase = getattr(api, 'passphrase', '')
                    self._start_okx(group, key, sec, passphrase, suf)
                else:
                    self._start_binance(group, key, sec, suf)

    # —— OKX WS ——
    def _start_okx(self, group, api_key, api_secret, passphrase, suffix):
        def on_msg(ws, raw):
            try:
                data = json.loads(raw)
            except ValueError:
                return
            # 心跳
            if data.get("event") == "ping":
                ws.send(json.dumps({"event":"pong"}))
                return
            # 只要 positions 频道
            if data.get("arg", {}).get("channel") != "positions":
                return
            rows = []
            for itm in data.get("data", []):
                inst = itm.get("instId")
                qty  = float(itm.get("pos", 0)) * tgt_api.inst_map.get(inst, 1)
                rows.append({
                    "exchange":    "OKX",
                    "symbol":      inst,
                    "qty":         qty,
                    "entry_price": float(itm.get("avgPx", 0)),
                    "side":        itm.get("posSide", ""),
                    "pnl_usdt":    float(itm.get("upl", 0)),
                    "pct_pnl":     float(itm.get("uplRatio", 0)) * 100,
                    "order_id":    inst + suffix
                })
            self.positions_cache[group][suffix] = rows

        # 登录订阅
        ts   = self._okx_time()
        path = "/users/self/verify"
        sign = self._okx_sign(ts, api_secret, path)
        login = json.dumps({"op":"login","args":[api_key, passphrase, ts, sign]})
        sub   = json.dumps({"op":"subscribe","args":[{"channel":"positions","instType":"SWAP"}]})

        url = "wss://ws.okx.com:8443/ws/v5/private"
        app = WebSocketApp(
            url,
            on_open=lambda ws: [ws.send(login), ws.send(sub)],
            on_message=on_msg
        )
        threading.Thread(target=app.run_forever, daemon=True).start()

    # —— Binance WS ——
    def _start_binance(self, group, api_key, api_secret, suffix):
        # 获取 listenKey (Futures REST API)
        key = self._get_blisten(api_key)
        if not key:
            logger.warning(f"[BINANCE] listenKey 请求失败，跳过 WebSocket 建连 (group={group}, suffix={suffix})")
            return
        url = f"wss://fstream.binance.com/ws/{key}"

        def on_msg(ws, raw):
            try:
                msg = json.loads(raw)
            except ValueError:
                return
            if msg.get("e") != "ACCOUNT_UPDATE":
                return
            rows = []
            for e in msg.get("a", {}).get("P", []):
                sym = e.get("s")
                pa  = float(e.get("pa", 0))
                ep  = float(e.get("ep", 0))
                ct  = self._binance_map.get(sym, 1)
                qty = pa * ct
                rows.append({
                    "exchange":    "BINANCE",
                    "symbol":      sym,
                    "qty":         qty,
                    "entry_price": ep,
                    "side":        "long" if qty > 0 else "short",
                    "pnl_usdt":    0.0,
                    "pct_pnl":     0.0,
                    "order_id":    f"{datetime.utcnow().timestamp()}{suffix}"
                })
            self.positions_cache[group][suffix] = rows

        app = WebSocketApp(url, on_message=on_msg)
        threading.Thread(target=app.run_forever, daemon=True).start()

    # —— 辅助方法 ——
    def _okx_time(self):
        try:
            r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
            ts = r.json().get("data", [])[0].get("ts")
            ms = int(ts)
        except Exception:
            ms = int(time.time() * 1000)
        return datetime.utcfromtimestamp(ms / 1000.0).isoformat("T", "milliseconds") + "Z"

    def _okx_sign(self, ts, secret, path):
        pre = ts + "GET" + path
        h   = hmac.new(secret.encode(), pre.encode(), hashlib.sha256)
        return base64.b64encode(h.digest()).decode()

    def _get_blisten(self, api_key):
        """调用 Binance Futures REST 接口获取 listenKey"""
        try:
            resp = requests.post(
                "https://fapi.binance.com/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": api_key},
                timeout=5
            )
            resp.raise_for_status()
            return resp.json().get("listenKey")
        except Exception as e:
            logger.error(f"[BINANCE] listenKey 请求失败（网络错误）：{e}")
            return None

    def _load_binance_inst_map(self):
        """调用 Binance Futures REST 接口获取合约面值映射"""
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=5
            )
            resp.raise_for_status()
            syms = resp.json().get("symbols", [])
            return {
                s["symbol"]: float(s.get("contractSize", 1))
                for s in syms if s.get("contractType") == "PERPETUAL"
            }
        except Exception as e:
            logger.error(f"[BINANCE] exchangeInfo 获取失败：{e}")
            return {}
