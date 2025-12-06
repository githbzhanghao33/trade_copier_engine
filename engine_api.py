# engine_api.py

import time
import json
import uuid
from flask import request, jsonify, Response, stream_with_context


def register_api(app, group_engines, broker):
    """
    在已有 Flask app 上注册：
      1. POST /api/manual_close       手动平仓接口
      2. GET  /api/stream/positions    SSE 实时推送持仓，仅按分组推所有后缀
    """

    def gen_order_id():
        ts = int(time.time() * 1000)
        return f"{ts}-{uuid.uuid4().hex}"

    @app.route('/api/manual_close', methods=['POST'])
    def api_manual_close():
        data = request.get_json(force=True)
        group    = data.get('group', 'default')
        symbol   = data.get('symbol')
        qty      = data.get('qty')
        tp       = data.get('tp')
        sl       = data.get('sl')
        order_id = gen_order_id()

        engine = group_engines.get(group)
        if not engine:
            return jsonify({'code': 404, 'msg': '该分组尚未启动'}), 404

        try:
            result = engine.close_position(
                symbol=symbol,
                qty=qty,
                tp=tp,
                sl=sl,
                order_id=order_id,
                manual=True
            )
            status = result.status
            msg    = result.msg
        except Exception as e:
            status = 'failed'
            msg    = str(e)

        return jsonify({
            'code':     0,
            'order_id': order_id,
            'status':   status,
            'msg':      msg
        })

    @app.route('/api/stream/positions')
    def stream_positions():
        """
        SSE 实时推送当前客户端的所有持仓，仅按分组推送，无后缀过滤：
          GET /api/stream/positions?group=<group>
        推送 JSON:
          { "positions": [ {...}, ... ] }
        """
        group = request.args.get('group', 'default')

        def event_stream():
            while True:
                # 从 WSBroker 缓存中读取该分组所有后缀的最新持仓
                data_for_group = broker.positions_cache.get(group, {})
                positions = []
                # data_for_group: suffix -> list of positions
                for pos_list in data_for_group.values():
                    positions.extend(pos_list)
                payload = {'positions': positions}
                yield f"data: {json.dumps(payload)}\n\n"
                time.sleep(1)

        return Response(
            stream_with_context(event_stream()),
            content_type='text/event-stream'
        )
