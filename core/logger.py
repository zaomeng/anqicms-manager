from __future__ import annotations

import logging
from pathlib import Path


class UILogHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        self.callback(self.format(record))


def configure_logging(log_file: str | Path = "fabu.log", callback=None) -> logging.Logger:
    logger = logging.getLogger("anqicms")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if callback:
        ui_handler = UILogHandler(callback)
        ui_handler.setFormatter(formatter)
        logger.addHandler(ui_handler)
    return logger
