# core/engine.py

from decimal import Decimal
from core.precision import PrecisionManager
import threading
import time
import os
from core.engine_run import run_loop
import json
import logging
from typing import Dict, List, Tuple, Any
import traceback
from utils.logger import setup_logger, short_key
from core.api_okx import OkxAPI
from core.api_binance import BinanceAPI
from core.api_gate import GateAPI

HEARTBEAT_INTERVAL = 60  # 秒
SLIPPAGE_THRESHOLD_DEFAULT = 0.5  # 百分比 (0.5%)
MAX_SINGLE_DEFAULT = float('inf')
MAX_TOTAL_DEFAULT = float('inf')
RETRY_COUNT = 3
RETRY_DELAY = 1  # 秒

DEAD_LETTER_FILE = "dead_letter.log"


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 3, reset_interval: int = 600):
        self.fail_count = 0
        self.fail_threshold = fail_threshold
        self.last_fail_time = 0.0
        self.reset_interval = reset_interval

    def record_fail(self):
        self.fail_count += 1
        self.last_fail_time = time.time()

    def record_success(self):
        self.fail_count = 0
        self.last_fail_time = 0.0

    def is_tripped(self) -> bool:
        if self.fail_count >= self.fail_threshold:
            if time.time() - self.last_fail_time < self.reset_interval:
                return True
            else:
                # 重置熔断器
                self.fail_count = 0
                return False
        return False


class TradeCopierEngine(threading.Thread):
    def __init__(
        self,
        source_info: Dict,
        targets_info: List[Dict],
        group_name: str
    ):
        super().__init__()
        self.source_info = source_info
        self.targets_info = targets_info
        self.group_name = group_name
        self.last_pos = {}
        self.manual_closed = set()
        self.monitored = set()

        
        # （1）创建“组级别”日志，用于记录引擎整体状态/心跳/通用事件
        group_log = f"logs/{group_name}.log"
        os.makedirs(os.path.dirname(group_log), exist_ok=True)
        self.logger = setup_logger(
            name=f"Engine_{group_name}",
            log_file=group_log
        )

        # （2）初始化 source_api 时，为源端单独创建 Logger，并加“源<交易所> <后四位>”前缀
         # —— 在这里加上 ——
        self.balances = {}               # 存放各目标端余额
        self.poll_interval = 0.5         # 循环间隔
        self.error_backoff = 1.0         # 异常重试延迟
        self.last_pos = {}               # 持仓差值检测用
        self.manual_closed = set()       # 平仓屏蔽开仓用
        self.monitored = set()           # 动态收集监控的合约
        src_key = source_info["api_key"]
        src_key_short = short_key(src_key)
        # 比如 logs/groupA_source_ABCD.log
        src_log_path = f"logs/{group_name}_源{source_info['name']}_{src_key_short}.log"
        os.makedirs(os.path.dirname(src_log_path), exist_ok=True)
        src_logger = setup_logger(
            name=f"{group_name}_源{source_info['name']}_{src_key_short}",
            log_file=src_log_path
        )

        plat = source_info["name"].lower()
        if plat == "okx":
            cls = OkxAPI
            args = {
            "api_key":    source_info["api_key"],
            "api_secret": source_info["api_secret"],
            "passphrase": source_info.get("passphrase", ""),
            "logger":     src_logger
            }
        elif plat == "binance":
            cls = BinanceAPI
            args = {
            "api_key":    source_info["api_key"],
            "api_secret": source_info["api_secret"],
            "logger":     src_logger
            }
        elif plat == "gate":
            cls = GateAPI
            args = {
            "api_key":    source_info["api_key"],
            "api_secret": source_info["api_secret"],
            "logger":     src_logger
            }
        else:
            raise ValueError(f"不支持的源端平台：{plat}")

        self.source_api = cls(**args)
        # （3）初始化 targets_api，为每个目标交易所创建“跟<交易所> <后四位>” Logger
        self.targets = []
        self.circuit_breakers: Dict[Tuple[str, str], CircuitBreaker] = {}
        for t in targets_info:
            name = t["name"].lower()
            api_key = t["api_key"]
            api_secret = t["api_secret"]
            passphrase = t.get("passphrase", "")
            leverage = t.get("leverage", 1)
            follow_ratio = t.get("ratio", 1.0)
            tp_ratio = t.get("tp", 0.0)
            sl_ratio = t.get("sl", 0.0)
            allow_slip = t.get("allow_slippage", SLIPPAGE_THRESHOLD_DEFAULT)
            max_single = t.get("max_single", MAX_SINGLE_DEFAULT)
            max_total = t.get("max_total", MAX_TOTAL_DEFAULT)

            key_short = short_key(api_key)
            tgt_log_path = f"logs/{group_name}_跟{name}_{key_short}.log"
            os.makedirs(os.path.dirname(tgt_log_path), exist_ok=True)
            tgt_logger = setup_logger(
                name=f"{group_name}_跟{name}_{key_short}",
                log_file=tgt_log_path
            )

            if name == "okx":
                api = OkxAPI(
                    api_key=api_key,
                    api_secret=api_secret,
                    passphrase=passphrase,
                    logger=tgt_logger
                )
            elif name == "binance":
                api = BinanceAPI(
                    api_key=api_key,
                    api_secret=api_secret,
                    logger=tgt_logger
                )
            elif name == "gate":
                api = GateAPI(
                    api_key=api_key,
                    api_secret=api_secret,
                    logger=tgt_logger
                )
            else:
                raise ValueError(f"不支持的目标平台：{t['name']}")

            self.targets.append({
                "name": name,
                "api": api,
                "leverage": leverage,
                "follow_ratio": follow_ratio,
                "tp_ratio": tp_ratio,
                "sl_ratio": sl_ratio,
                "allow_slippage": allow_slip,
                "max_single": max_single,
                "max_total": max_total,
            })
        self.precision = PrecisionManager()

        # 拉所有交易所的精度
        plat = self.source_info["name"].lower()
        if plat == "okx":
            self.precision.load_okx(
            base_url=self.source_api._base_url
            )
        elif plat == "binance":
            self.precision.load_binance(
            base_url=self.source_api._base_url,
            api_key=self.source_api.api_key
            )
        elif plat == "gate":
            self.precision.load_gate(
            base_url=self.source_api._base_url
            )
        else:
            raise ValueError(f"不支持的源端平台：{plat}")

# 再为每个 target 平台加载对应精度
        for tgt in self.targets:
            name = tgt["name"].lower()
            api  = tgt["api"]
            if name == "okx":
                self.precision.load_okx(
                base_url=api._base_url
                )
            elif name == "binance":
                self.precision.load_binance(
                base_url=api._base_url,
                api_key=api.api_key
                )
            elif name == "gate":
                self.precision.load_gate(
                base_url=api._base_url
                )
            else:
        # 如果有意外平台也能快速发现
                raise ValueError(f"不支持的目标端平台：{name}")

        # 控制线程运行
        self._running = threading.Event()
        self._running.set()

        # 最近处理的源订单时间戳，用于去重
        self.last_src_ts = 0

    def run(self):
        run_loop(self)
    def stop(self):
        self._running.clear()

    def _write_heartbeat(self):
        hb_file = os.path.join("heartbeat", f"{self.group_name}_heartbeat.txt")
        with open(hb_file, "w") as f:
            f.write(str(time.time()))
        self.logger.debug(f"[ENGINE] 写入心跳文件: {hb_file}")

    def _process_order(self, symbol: str, side: str, qty: float, price: float, tgt_cfg: Dict):
        tgt_name = tgt_cfg["name"]
        api = tgt_cfg["api"]
        lever = tgt_cfg["leverage"]
        ratio = tgt_cfg["follow_ratio"]
        tp = tgt_cfg["tp_ratio"]
        sl = tgt_cfg["sl_ratio"]
        allow_slip = tgt_cfg["allow_slippage"]
        max_single = tgt_cfg["max_single"]
        max_total = tgt_cfg["max_total"]

        self.logger.info(f"[下单调用] {tgt_name}: symbol={symbol}, side={side}, qty={qty}, price={price}")

        if price is None:
            try:
                bid, ask = api.get_best_price(symbol)
                price = ask if side.lower() == "buy" else bid
                self.logger.debug(f"[护栏] 未指定 price，使用实价 {price:.4f}")
            except Exception as e:
                self.logger.error(f"[护栏] 获取实价失败，跳过本次下单: {e}")
                return

     
        # 熔断器检查
        cb_key = (tgt_name, symbol)
        breaker = self.circuit_breakers.setdefault(cb_key, CircuitBreaker())
        if breaker.is_tripped():
            self.logger.warning(f"跟{tgt_name} {tgt_cfg['api'].api_key[-4:]} 熔断中，跳过本次跟单")
            return

        # 计算下单量
        # 计算张数和本次面值（USDT）交易所的价格和数量精度向下截断
        order_qty = qty * ratio
        # —— 调试开始 —— 
# 1) 打印 Gate 规则条目数
        self.logger.debug(f"[DEBUG rule] Gate rules count: {len(self.precision.rules['gate'])}")

# 2) 打印前 5 个规则 key
        self.logger.debug(f"[DEBUG rule] Gate sample keys: {list(self.precision.rules['gate'].keys())[:5]}")

# 3) 打印本次下单用的 symbol，以及它在规则表中的匹配情况
        self.logger.debug(
        f"[DEBUG map] symbol for lookup: {symbol!r}, "
        f"exists in rules? {symbol in self.precision.rules['gate']}"
        )
    # —— 调试结束 ——
        self.logger.debug(
        f"[DEBUG order] symbol={symbol}, "
        f"delta_qty={qty:.6f}, ratio={ratio}, "
        f"raw_order_qty={order_qty:.6f}"
        )
        safe_qty, safe_price = self.precision.get_truncated(
            exchange=tgt_name.lower(),  # "binance" / "gate" / "okx"
            symbol=symbol,
            raw_qty=order_qty,
            raw_price=price
        )
        self.logger.debug(
        f"[DEBUG order] safe_qty={safe_qty:.6f}, safe_price={safe_price}"
        )
        if safe_qty <= 0:
            self.logger.info(
        f"[{tgt_name}] {symbol} raw_qty={order_qty:.6f} 截断后 safe_qty=0，跳过下单"
            )
            return
        order_qty = safe_qty
        price     = safe_price


        notional  = order_qty * price
        # 单笔 USDT 限额
        if notional > max_single:
            api.logger.warning(
        f"跟{tgt_name} {api.api_key[-4:]} 单笔限额 {max_single}USDT，"
        f"本次面值 {notional:.2f}USDT > 限额，跳过"
            )
            return

       # 查询当前持仓张数并计算面值
        try:
           current_pos = api.get_position_qty(symbol)
        except Exception as e:
           api.logger.error(f"跟{tgt_name} {api.api_key[-4:]} 查询持仓失败: {e}")
           breaker.record_fail()
           return

        current_notional = abs(current_pos) * price

       # 累计 USDT 限额
        if current_notional + notional > max_total:
            api.logger.warning(
        f"跟{tgt_name} {api.api_key[-4:]} 累计限额 {max_total}USDT，"
        f"当前面值 {current_notional:.2f}USDT + 本次 {notional:.2f}USDT > 限额，跳过"
           )
            return

        # 滑点检查
        try:
            best_bid, best_ask = api.get_best_price(symbol)
        except Exception as e:
            api.logger.error(f"跟{tgt_name} {api.api_key[-4:]} 获取最优价失败: {e}")
            breaker.record_fail()
            return

        slip = 0.0
        if side.lower() == "buy":
            slip = (best_ask - price) / price * 100
            if slip > allow_slip:
                api.logger.warning(
                    f"跟{tgt_name} {api.api_key[-4:]} 买入滑点 {slip:.2f}% > {allow_slip}% ，跳过"
                )
                return
            order_price = best_ask
        else:
            slip = (price - best_bid) / price * 100
            if slip > allow_slip:
                api.logger.warning(
                    f"跟{tgt_name} {api.api_key[-4:]} 卖出滑点 {slip:.2f}% > {allow_slip}% ，跳过"
                )
                return
            order_price = best_bid

        # 设置杠杆
        try:
            api.set_leverage(symbol=symbol, leverage=lever)
            api.logger.info(f"跟{tgt_name} {api.api_key[-4:]} 杠杆已设置为 {lever}x")
            # 若要在“组日志”再加一条记录，可写：
            self.logger.info(
                f"跟{tgt_name} {api.api_key[-4:]} 已为 {symbol} 设置杠杆 {lever}x"
            )
        except Exception as e:
            api.logger.error(f"跟{tgt_name} {api.api_key[-4:]} 设置杠杆失败: {e}")
            breaker.record_fail()
            return

        # 下单重试逻辑
        success = False
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                api.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=order_qty,
                    price=order_price,
                )
                success = True
                break
            except Exception as e:
                api.logger.error(f"跟{tgt_name} {api.api_key[-4:]} 第 {attempt} 次下单失败: {e}")
                time.sleep(RETRY_DELAY)

        if not success:
            api.logger.error(f"跟{tgt_name} {api.api_key[-4:]} 下单重试 {RETRY_COUNT} 次后失败，加入死信队列")
            breaker.record_fail()
            self._write_dead_letter(symbol, side, order_qty, price, tgt_name)
            return

        # 下单成功
        breaker.record_success()
        api.logger.info(f"跟{tgt_name} {api.api_key[-4:]} 下单成功: {symbol} {side} {order_qty} @ {order_price}")
        # 同时在组日志再写一条
        self.logger.info(
            f"跟{tgt_name} {api.api_key[-4:]} 下单成功: {symbol} {side} {order_qty} @ {order_price}"
        )

    def _write_dead_letter(self, symbol: str, side: str, qty: float, price: float, tgt_name: str):
        entry = {
            "timestamp": time.time(),
            "target": tgt_name,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
        }
        with open(DEAD_LETTER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def close_position(
        self,
        symbol: str,
        qty: float = None
    ) -> Dict[str, Any]:
        """
        对所有目标交易所统一执行市价平仓（手动或自动都可以调用）：
          1) 先查持仓，若 pos==0 则跳过
          2) 计算 close_qty = abs(pos) 或传入的 qty
          3) 调 precision.get_truncated 做数量截断
          4) safe_qty<=0 跳过
          5) 市价单下平仓
          6) 更新 self.last_pos[symbol]
        返回各目标平仓结果列表。
        """
        results = []
        for tgt in self.targets:
            tgt_name = tgt["name"]
            api      = tgt["api"]

            tgt_symbol = api.convert_symbol(symbol)

            # 1) 查询最新持仓
            try:
                pos = api.get_position_qty(tgt_symbol)
            except Exception as e:
                results.append({
                    "target": tgt_name,
                    "status": "error",
                    "msg": f"查询持仓失败: {e}"
                })
                continue

            # 2) 无持仓则跳过
            if pos == 0:
                results.append({
                    "target": tgt_name,
                    "status": "no_position",
                    "msg": "无持仓，跳过平仓"
                })
                # 同步更新基线
                self.last_pos[symbol] = 0.0
                continue

            # 3) 计算原始要平数量
            raw_qty = abs(pos) if qty is None else qty

            # 4) 截断到最小单位
            safe_qty, _ = self.precision.get_truncated(
                exchange  = tgt_name.lower(),
                symbol    = tgt_symbol,
                raw_qty   = raw_qty,
                raw_price = None
            )

            # 5) safe_qty<=0 跳过
            if safe_qty <= 0:
                results.append({
                    "target": tgt_name,
                    "status": "skipped",
                    "msg": f"{tgt_symbol} raw_close_qty={raw_qty:.6f} < 最小单位，跳过"
                })
                # 更新基线，防止下一轮再平
                self.last_pos[symbol] = pos
                continue

            # 6) 下市价平仓单
            side = "sell" if pos > 0 else "buy"
            try:
                api.place_order(
                    symbol      = tgt_symbol,
                    side        = side,
                    quantity    = safe_qty,
                    price       = None        # 市价单
                )
                results.append({
                    "target": tgt_name,
                    "status": "ok",
                    "msg": f"平仓 {safe_qty} 成功"
                })
                # 7) 成功后同步更新基线持仓
                new_pos = api.get_position_qty(tgt_symbol)
                self.last_pos[symbol] = new_pos

            except Exception as e:
                results.append({
                    "target": tgt_name,
                    "status": "error",
                    "msg": str(e)
                })
                # 不更新 self.last_pos，保留旧值以便重试

        return {"results": results}
