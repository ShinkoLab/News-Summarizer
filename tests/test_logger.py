"""HTTP transport logs must never emit secret-bearing request URLs."""
import logging
from unittest.mock import patch

import pytest

import logger as logging_module


@pytest.mark.parametrize('level, expected', [('INFO', logging.WARNING), ('DEBUG', logging.WARNING), ('ERROR', logging.ERROR)])
def test_http_transport_levels_never_allow_url_logs(monkeypatch, level, expected):
    root = logging.getLogger()
    handlers = list(root.handlers)
    monkeypatch.setattr(root, 'level', root.level)
    for name in ('httpx', 'httpcore', *logging_module._APP_NAMESPACES):
        log = logging.getLogger(name)
        monkeypatch.setattr(log, 'level', log.level)
    try:
        with patch.object(logging_module.config.logging, 'level', level):
            logging_module.setup_logging(debug=True)
        assert logging.getLogger('httpx').level == expected
        assert logging.getLogger('httpcore').level == expected
        assert logging.getLogger('pipeline').level == logging.DEBUG
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
