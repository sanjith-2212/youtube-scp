import logging
import os
import time
import random
import yaml
from pathlib import Path
from logging.handlers import RotatingFileHandler


def load_config():
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_channels():
    channels_path = Path(__file__).parent.parent / "config" / "channels.yaml"
    with open(channels_path) as f:
        return yaml.safe_load(f)


def setup_logger(name, config, lang=None):
    log_dir = Path(config["logging"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)

    if lang:
        log_file = log_dir / f"{lang}.log"
    else:
        log_file = log_dir / "main.log"

    logger = logging.getLogger(name)
    logger.setLevel(config["logging"]["level"])

    if not logger.handlers:
        fh = RotatingFileHandler(
            log_file,
            maxBytes=config["logging"]["rotate_mb"] * 1024 * 1024,
            backupCount=5,
        )
        fh.setLevel(config["logging"]["level"])

        ch = logging.StreamHandler()
        ch.setLevel(logging.WARNING)

        fmt = logging.Formatter(
            "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)

        logger.addHandler(fh)
        logger.addHandler(ch)

    return logger


def rate_limit_delay(config):
    delay = random.uniform(
        config["processing"]["request_delay_min"],
        config["processing"]["request_delay_max"],
    )
    time.sleep(delay)


def get_proxy_list(config):
    proxies = []
    for p in config.get("proxies", []):
        if p.get("address"):
            proxies.append(p["address"])
        else:
            proxies.append(None)
    return proxies if proxies else [None]
