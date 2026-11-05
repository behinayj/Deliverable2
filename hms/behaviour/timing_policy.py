"""1.3.1 Timing Policy: the system's timing and retention constants."""
from datetime import timedelta

STALE_LIMIT = timedelta(seconds=30)
TICK_PERIOD = 1.0  # seconds
POLL_PERIOD = 0.1  # seconds
RESPONSE_BOUND = 2.0  # seconds
UI_REFRESH = 1  # seconds
RETENTION = timedelta(days=90)
SESSION_TIMEOUT = timedelta(minutes=15)
DEFAULT_STATUS_PERIOD = timedelta(seconds=5)
