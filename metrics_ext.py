# metrics_ext.py
import time
from flask import jsonify

# 缓存机制：最多每 5 分钟更新一次
_metrics_cache = None
_metrics_last_update = 0
_metrics_ttl = 300  # 缓存有效期（秒）

def register_metrics(app, engine_status):
    @app.route("/api/metrics", methods=["GET"])
    def api_metrics():
        global _metrics_cache, _metrics_last_update
        now = time.time()
        # 缓存失效或首次请求时重新收集数据
        if _metrics_cache is None or now - _metrics_last_update > _metrics_ttl:
            # 统计总跟单人数
            total_followers = sum(
                getattr(engine, 'follower_count', 0)
                for engine in engine_status.values()
            )

            group_stats = []
            # 遍历每个分组的引擎实例
            for group, engine in engine_status.items():
                for tgt in getattr(engine, 'targets', []):
                    api        = tgt.get("api")
                    key        = tgt.get("api_key", "")
                    api_suffix = key[-4:] if key else None

                    # 1) 在线拉取目标端余额
                    balance = api.get_balance()

                    # 2) 在线拉取目标端全量持仓
                    pos_map = api.get_positions()  # { symbol: qty }

                    # 3) 计算该分组所有持仓的总盈亏
                    total_usdt = 0.0
                    for symbol, qty in pos_map.items():
                        # 如果有 entry_prices，可做精确计算，否则略过
                        entry = getattr(engine, 'entry_prices', {})
                        entry_price = entry.get(tgt.get('name'), {}).get(symbol)
                        if entry_price:
                            current_price = api.get_best_price(symbol)[0]
                            # 盈亏（USDT）= 余额 * pct_change
                            pct_change = (current_price - entry_price) / entry_price
                            total_usdt += balance * pct_change

                    total_pct = (total_usdt / balance) if balance else 0.0

                    group_stats.append({
                        "group":           group,
                        "api_key_suffix":  api_suffix,
                        "balance":         round(balance, 6),
                        "total_usdt_pnl":  round(total_usdt, 6),
                        "total_pct_pnl":   round(total_pct, 6)
                    })

            # 更新缓存
            _metrics_cache = {"code": 0, "data": {"follower_count": total_followers, "group_stats": group_stats}}
            _metrics_last_update = now

        return jsonify(_metrics_cache)
