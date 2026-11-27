import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading


def get_log_dir() -> str:
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    log_dir = os.path.join(base, "ScreenCamLite", "logs")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def get_log_file_path() -> str:
    return os.path.join(get_log_dir(), "screencam.log")


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("ScreenCamLite")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    log_path = get_log_file_path()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Uncaught main exception", exc_info=(exc_type, exc_value, exc_traceback))

    def handle_thread_exception(args):
        logger.critical(
            "Uncaught thread exception in %s",
            getattr(args, "thread", None),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = handle_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = handle_thread_exception

    logger.info("=== ScreenCam Lite started (frozen=%s) ===", getattr(sys, "frozen", False))
    return logger
