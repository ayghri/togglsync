import logging
import sys

import requests

logger = logging.getLogger(__name__)
formatter = logging.Formatter(
    "%(asctime)s (%(filename)s:%(lineno)d) %(levelname)s: %(message)s"
)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(formatter)
logger.addHandler(stdout_handler)

logger.setLevel(logging.DEBUG if verbose else logging.INFO)
# Load config
logger.debug("Loading config..")
