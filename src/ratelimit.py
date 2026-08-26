import fcntl
import json
import os
import random
import time
from pathlib import Path


class GlobalRateLimiter:
    """Paces downloads across every language pipeline at once.

    Each language runs in its own process, so a per-process sleep lets the
    aggregate request rate scale with the worker count -- which is how we
    walked into YouTube's subnet throttle. The budget therefore lives in a
    small JSON file guarded by flock, shared by all workers and surviving
    restarts.

    Two mechanisms share the file:
      - next_slot:     token bucket, one download per 60/rpm seconds globally
      - blocked_until: global cooldown after a bot check, with escalating
                       backoff so repeated hits back off harder
    """

    def __init__(self, config, logger):
        self.logger = logger
        p = config["processing"]
        rpm = float(p.get("global_requests_per_min", 2.5))
        self.min_interval = 60.0 / max(rpm, 0.01)
        self.backoff_base = float(p.get("ratelimit_backoff_base_sec", 900))
        self.backoff_max = float(p.get("ratelimit_backoff_max_sec", 5400))
        self.strike_reset = float(p.get("ratelimit_strike_reset_sec", 1800))

        state_dir = Path(config["local_staging"]["dir"])
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / ".rate_limit_state.json"

    DEFAULT_STATE = {"next_slot": 0.0, "blocked_until": 0.0, "strikes": 0, "last_block": 0.0}

    def _read(self, fh):
        fh.seek(0)
        raw = fh.read()
        if not raw.strip():
            return dict(self.DEFAULT_STATE)
        try:
            state = dict(self.DEFAULT_STATE)
            state.update(json.loads(raw))
            return state
        except ValueError:
            # A torn write from a killed worker shouldn't wedge the run.
            return dict(self.DEFAULT_STATE)

    def _write(self, fh, state):
        fh.seek(0)
        fh.truncate()
        json.dump(state, fh)
        fh.flush()
        os.fsync(fh.fileno())

    def acquire(self):
        """Block until this worker may issue one download."""
        while True:
            with open(self.state_path, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    state = self._read(fh)
                    now = time.time()
                    wait = max(state["blocked_until"] - now, state["next_slot"] - now, 0.0)
                    if wait <= 0:
                        # Jitter keeps the workers from re-converging into lockstep.
                        state["next_slot"] = now + self.min_interval * random.uniform(0.85, 1.15)
                        self._write(fh, state)
                        return
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

            # Sleep with the lock released, in slices so a cooldown lifted by
            # another worker is picked up promptly.
            time.sleep(min(wait, 30.0))

    def penalize(self):
        """Record a bot check and pause every worker. Returns the cooldown in seconds."""
        with open(self.state_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                state = self._read(fh)
                now = time.time()

                # Workers hit the wall in a pack; only the first one escalates,
                # the rest just inherit the cooldown already in force.
                if now < state["blocked_until"]:
                    return state["blocked_until"] - now

                if now - state["last_block"] > self.strike_reset:
                    state["strikes"] = 0

                state["strikes"] = min(state["strikes"] + 1, 8)
                backoff = min(self.backoff_base * (2 ** (state["strikes"] - 1)), self.backoff_max)
                state["blocked_until"] = now + backoff
                state["last_block"] = now
                self._write(fh, state)
                return backoff
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def cooldown_remaining(self):
        """Seconds left on any active global cooldown (0 if clear)."""
        with open(self.state_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                state = self._read(fh)
                return max(state["blocked_until"] - time.time(), 0.0)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
