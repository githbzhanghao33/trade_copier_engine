# core/engine_run.py
import time

HEARTBEAT_INTERVAL = 60  # 心跳间隔（秒）

def run_loop(engine):
    """
    动态差值驱动主循环：
    1. 每次拉取所有合约持仓快照 get_positions()
    2. 对比前次快照计算 delta
    3. delta != 0 时触发 open/close 跟单
       - open 调 engine._process_order
       - close 统一走 engine.close_position
    4. 更新快照并写心跳
    """
    # 1) 初始化快照
    engine.last_pos = engine.source_api.get_positions() or {}
    #engine.logger.info(
    #    f"[ENGINE] Dynamic-delta engine started; monitoring {len(engine.last_pos)} symbols"
    #)

    next_hb = time.time() + HEARTBEAT_INTERVAL
    while engine._running.is_set():
        now = time.time()
        if now >= next_hb:
            engine._write_heartbeat()
            next_hb = now + HEARTBEAT_INTERVAL

        try:
            # 2) 拉当前全量持仓快照
            curr_pos = engine.source_api.get_positions() or {}

            # 3) 比对所有可能变动的合约
            all_syms = set(engine.last_pos.keys()) | set(curr_pos.keys())
            for sym in all_syms:
                prev  = engine.last_pos.get(sym, 0.0)
                curr  = curr_pos.get(sym,    0.0)
                delta = curr - prev

               # engine.logger.debug(f"[Delta] {sym}: prev={prev}, curr={curr}, delta={delta}")
                if delta == 0:
                    continue

                action = "open" if abs(curr) > abs(prev) else "close"
                qty    = abs(delta)

                if action == "open":
                    # 开仓走原来风控下单逻辑
                    for tgt in engine.targets:
                        api     = tgt["api"]
                        tgt_sym = api.convert_symbol(sym)
                        side    = "buy" if delta > 0 else "sell"
                        engine._process_order(
                            symbol  = tgt_sym,
                            side    = side,
                            qty     = qty,
                            price   = None,
                            tgt_cfg = tgt
                        )
                else:
                    # 平仓统一走 close_position
                    engine.logger.info(f"[自动平仓] {sym} Δ={delta}, 调用 close_position")
                    res = engine.close_position(
                        symbol = sym,
                        qty    = qty
                    )
                    # 打印每个目标执行结果
                    for r in res.get("results", []):
                        tgt_name = r["target"]
                        status   = r["status"]
                        msg      = r.get("msg", "")
                        if status == "ok":
                            engine.logger.info(f"[自动平仓][{tgt_name}] {sym} 平仓成功")
                        else:
                            engine.logger.warning(f"[自动平仓][{tgt_name}] {sym} 平仓{status}: {msg}")

                # 4) 更新基线快照
                engine.last_pos[sym] = curr

        except Exception:
            engine.logger.error("[ENGINE] 主循环异常", exc_info=True)

        time.sleep(engine.poll_interval)

    engine.logger.info("[ENGINE] Dynamic-delta engine stopped")
