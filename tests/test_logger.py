import logging

from core.logger import configure_logging


def test_configure_logging_closes_replaced_handlers(tmp_path):
    old = configure_logging(tmp_path / "old.log").handlers[0]

    configure_logging(tmp_path / "new.log")

    assert old.stream is None
    assert (tmp_path / "new.log").exists()
    logging.getLogger("anqicms").handlers.clear()
