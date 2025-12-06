# utils/logger.py
import logging
import os

def setup_logger(name: str, log_file: str, level: int = logging.DEBUG) -> logging.Logger:
    """
    创建一个同时输出到文件和控制台的 Logger。
    - name: logger 名称
    - log_file: 日志文件路径（工具会自动创建目录）
    - level: 日志级别，默认为 DEBUG，保证所有 DEBUG/INFO 日志都会打印
    """
    # 确保日志路径目录存在
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # 禁止向上传播到 root logger

    # 仅在第一次添加 handler 时创建
    if not logger.handlers:
        # 文件 Handler
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(level)
        fh_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        fh.setFormatter(fh_formatter)
        logger.addHandler(fh)

        # 控制台 Handler
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        ch.setFormatter(ch_formatter)
        logger.addHandler(ch)

    return logger


def short_key(api_key: str, length: int = 4) -> str:
    """
    从完整的 API Key 中截取后 length 位，用作“掩码”标识。
    比如：'abcd1234EFGH' -> 'EFGH'（length=4）
    如果 api_key 长度不足 length，则直接返回它本身。
    """
    if not api_key:
        return ""
    return api_key[-length:] if len(api_key) > length else api_key
