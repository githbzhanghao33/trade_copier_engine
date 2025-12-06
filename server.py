# server.py
from core.ws_broker import WSBroker
from typing import Dict
from utils.auth import get_group_by_auth_code
from sqlalchemy import update
from models.db import SessionLocal, engine, Base
from models.config_model import Config
from utils.auth import require_auth_code
from utils.config_utils import get_config
import engine_api
import threading
import time
import logging
import json
from metrics_ext import register_metrics
from flask import Flask, request, jsonify
from core.engine import TradeCopierEngine
from core.api_okx import OkxAPI
from core.api_binance import BinanceAPI
from core.api_gate import GateAPI
import os

Base.metadata.create_all(engine)

app = Flask(__name__)
# --------------------------- 全局常量和目录初始化 ---------------------------
logging.basicConfig(level=logging.INFO)

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)


# 有效交易所平台集合
VALID_PLATFORMS = {"okx", "binance", "gate"}

# 风控参数范围（如果后续你想在 validate_config 里校验比率、杠杆等）
LEVERAGE_MIN, LEVERAGE_MAX = 1, 125
RATIO_MIN, RATIO_MAX = 0.0, 1.0
SLIPPAGE_MAX = 5.0  # 最多允许 5% 的滑点
MAX_SINGLE_MAX = float("inf")
MAX_TOTAL_MAX = float("inf")

# 全局存储：每个分组的引擎实例，以及运行状态
GROUP_ENGINES = {}       # e.g. GROUP_ENGINES["groupA"] = TradeCopierEngine(...)
ENGINE_STATUS = {}       # e.g. ENGINE_STATUS["groupA"] = "running" / "auth_failed: ..."

# ----------------------------------------------------------------------------

DEFAULT_PARAMS = {
    "leverage":       1,
    "follow_ratio":   1.0,
    "tp_ratio":       0.1,
    "sl_ratio":       0.05,
    "allow_slippage": 0.5,
    "max_single":     1000,
    "max_total":      5000
}
# 各平台必填字段
REQUIRED_FIELDS = {
    "okx":     ["api_key", "api_secret", "passphrase"],
    "binance": ["api_key", "api_secret"],
    "gate":    ["api_key", "api_secret"]
}

def deep_update(dst: dict, src: dict) -> dict:
    """递归地把 src 合并到 dst 上，返回 dst。"""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_update(dst[k], v)
        else:
            dst[k] = v
    return dst

def clean_exchanges(exs: Dict[str, dict]) -> Dict[str, dict]:
    """
    去掉那些 api_key/api_secret 为空的交易所配置。
    """
    return {
        name: creds
        for name, creds in exs.items()
        if creds.get("api_key") and creds.get("api_secret")
    }

@app.route('/api/config', methods=['GET'])
def api_get_config():
    auth_code, group = require_auth_code()
    try:
        data = get_config(group)
    except ValueError:
        data = {
            "exchanges": {},
            "settings": {}
        }
    return jsonify({
        "code": 0,
        "data": data
    })
@app.route('/api/config', methods=['POST'])
def api_post_config():
    auth_code, group = require_auth_code()
    payload = request.get_json(force=True)

    # 1) 校验 exchanges、必填字段（同之前）…
    exchanges = payload.get('exchanges')
    if not isinstance(exchanges, dict):
        return jsonify({'code':400,'msg':'exchanges 必须是对象'}), 400
    for ex_name, creds in exchanges.items():
        reqs = REQUIRED_FIELDS.get(ex_name)
        if not reqs:
            return jsonify({'code':400,'msg':f'不支持的交易所: {ex_name}'}), 400
        for f in reqs:
            if f not in creds:
                return jsonify({'code':400,'msg':f'{ex_name} 缺少字段: {f}'}), 400

    # 2) 合并默认参数到 settings.*_params（同之前）…
    settings = payload.get('settings', {}) or {}
    for key, val in list(settings.items()):
        if key.endswith('_params'):
            merged = DEFAULT_PARAMS.copy()
            if isinstance(val, dict):
                merged.update(val)
            settings[key] = merged
    payload['settings'] = settings

    # 3) 深度合并到已有配置
    old = {}
    try:
        old = get_config(group)
    except ValueError:
        pass
    merged = deep_update(old, payload)

    merged['exchanges'] = clean_exchanges(merged.get('exchanges', {}))
    logging.info(f"[POST /api/config] 合并后配置: {merged}")

    # 4) 写入数据库
    db = SessionLocal()
    db.query(Config).filter_by(auth_code=auth_code).update({'data': merged})
    db.commit()

    if group in GROUP_ENGINES:
        old_engine = GROUP_ENGINES[group]
        old_engine.stop()              # 先发退出信号
        old_engine.join(timeout=5)     # 等它跑完最后几步，真正退出
        del GROUP_ENGINES[group]
        ENGINE_STATUS[group] = 'stopped'
        app.logger.info(f"[POST /api/config] 已停止旧引擎：{group}")

    # 5) 启动/重载引擎
    try:
        reload_engine(group, merged)
        logging.info(f"[POST /api/config] 引擎重载成功: {group}")
        broker.group_engines = GROUP_ENGINES
        # 如果你实现了 _start_all()，直接调用；否则针对这个 group 调用 _start_okx/_start_binance
        broker._start_all()
        logging.info("[POST /api/config] WSBroker 重新订阅完成")

        # —— 清空 metrics 缓存 —— 
        import metrics_ext
        metrics_ext._metrics_cache = None
        metrics_ext._metrics_last_update = 0
        logging.info("[POST /api/config] metrics 缓存已清空，下次请求会重新拉数据")
    except Exception as e:
        logging.exception("[POST /api/config] 引擎启动失败")
        return jsonify({'code':500, 'msg':f'引擎启动失败: {e}'}), 500

    return jsonify({'code':0, 'msg':'配置已保存并启动引擎'})
def get_group_log_path(group):
    return os.path.join(LOG_DIR, f"{group}.log")

# --------------------------- 配置校验函数 ---------------------------


# --------------------------- 路由：/api/auth ---------------------------

@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json(force=True) or {}
    code = data.get("auth_code")
    if not code:
        return jsonify({"valid": False, "message": "缺少 auth_code"}), 400

    group = get_group_by_auth_code(code)
    if not group:
        return jsonify({"valid": False, "message": "授权失败"}), 401

    return jsonify({"valid": True, "group": group})
# --------------------------- 路由：/api/config ---------------------------


    # ---------- POST 部分：上传并校验 + 连通性/鉴权测试 ----------

    # —— 1. 对“源端”做连通性 + 鉴权测试 ——

# --------------------------- 路由：/api/logs ---------------------------

@app.route("/api/logs", methods=["GET"])
def api_logs():
    auth_code, group = require_auth_code()
    if not group:
        return jsonify({"code":400, "msg":"缺少参数 group"}), 400

    log_path = get_group_log_path(group)
    if not os.path.exists(log_path):
        return jsonify({"code":404, "msg":"日志文件不存在"}), 404
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()[-200:]
    return jsonify({"code":0, "data":"".join(lines)})

# --------------------------- 路由：/api/logs/api_key ---------------------------

@app.route("/api/logs/api_key", methods=["GET"])
def api_logs_api_key():
    """
    客户端通过 ?group=xxx&exchange=yyy&key_short=zzzz 获取某个 API Key 的日志：
      - 如果 exchange == settings.source → logs/{group}_源{exchange}_{key_short}.log
      - 否则 → logs/{group}_跟{exchange}_{key_short}.log
    """
    auth_code, group      = require_auth_code()
    if not group:
        return jsonify({"code":400, "msg":"缺少参数 group"}), 400

    exchange   = request.args.get("exchange")    # okx / binance / gate
    key_short  = request.args.get("key_short")   # API Key 后四位掩码

    if not exchange or not key_short:
        return jsonify({"code":400, "msg":"缺少参数：exchange 或 key_short"}), 400

    # 先加载 configs/{group}.yaml，拿到 settings.source
    try:
        cfg = get_config(group)
    except ValueError:
        return jsonify({"code":404, "meg":"该分组配置不存在"}),404

    settings = cfg.get("settings", {})
    source_platform = settings.get("source", "").lower()
    ex_lower = exchange.lower()

    if ex_lower == source_platform:
        # 读取“源”日志
        log_filename = os.path.join(LOG_DIR, f"{group}_源{ex_lower}_{key_short}.log")
    else:
        # 读取“跟”日志
        log_filename = os.path.join(LOG_DIR, f"{group}_跟{ex_lower}_{key_short}.log")

    if not os.path.exists(log_filename):
        return jsonify({"code":404, "msg":"日志文件不存在"}), 404

    try:
        with open(log_filename, "r", encoding="utf-8") as f:
            lines = f.readlines()[-200:]
    except Exception as e:
        return jsonify({"code":500, "msg":f"无法读取日志文件: {e}"}), 500

    return jsonify({"code":0, "data":"".join(lines)})

# --------------------------- 路由：/api/status ---------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    """
    返回某分组引擎的当前状态：
      - running
      - auth_failed: ...（鉴权失败）
      - stopped
      - not started
    """
    group = request.args.get("group")
    if not group:
        return jsonify({"code":400, "msg":"缺少参数 group"}), 400

    status = ENGINE_STATUS.get(group)
    if status is None:
        return jsonify({"code":404, "msg":"该分组尚未启动"}), 404
    return jsonify({"code":0, "status":status})

# --------------------------- reload_engine 函数 ---------------------------

def reload_engine(group, cfg):
    """
    启动或重载某分组的跟单引擎
    """
    # 如果已有旧引擎，先停止、更新状态
    if group in GROUP_ENGINES:
        try:
            GROUP_ENGINES[group].stop()
        except Exception:
            pass
    ENGINE_STATUS[group] = "starting"

    # 组装 source_info
    exchanges = cfg.get("exchanges", {})
    settings = cfg.get("settings", {})
    source_name = settings.get("source", "").lower()
    if source_name not in exchanges:
        raise ValueError("settings.source 对应的交易所配置未找到")
    src_creds = exchanges.get(source_name, {})

    source_info = {
        "name": source_name,
        "api_key":    src_creds.get("api_key"),
        "api_secret": src_creds.get("api_secret"),
        "passphrase": src_creds.get("passphrase", ""),
    }

    # 组装 targets_info 列表
    targets_info = []
    for idx in [1, 2]:
        tname = settings.get(f"target{idx}", "None")
        if tname and tname.lower() != "none":
            tname_lower = tname.lower()
            if tname_lower not in exchanges:
                raise ValueError(f"settings.target{idx} 对应的交易所配置未找到")
            tcreds = exchanges.get(tname_lower, {})
            tparams = settings.get(f"target{idx}_params", {})

            tinfo = {
                "name":         tname_lower,
                "api_key":      tcreds.get("api_key"),
                "api_secret":   tcreds.get("api_secret"),
                "passphrase":   tcreds.get("passphrase", ""),
                "leverage":     tparams.get("leverage"),
                "ratio":        tparams.get("follow_ratio"),
                "tp":           tparams.get("tp_ratio"),
                "sl":           tparams.get("sl_ratio"),
                "allow_slippage":tparams.get("allow_slippage", SLIPPAGE_MAX),
                "max_single":   tparams.get("max_single", MAX_SINGLE_MAX),
                "max_total":    tparams.get("max_total", MAX_TOTAL_MAX),
            }
            targets_info.append(tinfo)

    # 启动新引擎线程
    engine = TradeCopierEngine(source_info, targets_info, group)
    engine.daemon = True
    engine.start()
    GROUP_ENGINES[group] = engine

   
# --------------------------- 主程序入口：启动时加载所有已存在配置 ---------------------------
if __name__ == "__main__":
    db = SessionLocal()
    for cfg in db.query(Config).all():
        try:
            reload_engine(cfg.group, cfg.data)
            print(f"已启动引擎：{cfg.group}")
        except Exception as e:
            print(f"启动分组 {cfg.group} 引擎失败：{e}")
    broker = WSBroker(GROUP_ENGINES)
    engine_api.register_api(app, GROUP_ENGINES, broker)
    register_metrics(app, GROUP_ENGINES)

    app.run(host="0.0.0.0", port=8080, debug=False)
