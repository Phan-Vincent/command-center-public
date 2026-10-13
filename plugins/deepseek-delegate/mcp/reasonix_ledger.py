"""Durable objective accounting. Derived from external GPT-6.1 Sol High draft.

Finish evidence is internal engine data, never a public request argument.
"""
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager

MAX_BYTES = 65536
HASH = re.compile(r"[0-9a-f]{64}\Z")
STATUSES = {"stopped", "not_started", "running", "unknown", "not_applicable"}
SCOPES = {"owned_local_process_group", "owned_remote_process_group", "not_started", "unknown"}
PAIRS = {"success": {"success"}, "substantive": {"failed"},
         "infrastructure": {"failed"}, "blocked": {"blocked"},
         "timeout": {"failed", "unknown"}, "cancelled": {"cancelled"},
         "unknown": {"failed", "unknown"}, "scope": {"failed"}}
ATTEMPT_KEYS = {"tier", "reason", "contract_hash", "kind", "phase", "outcome",
                "classification", "process", "remaining_budget"}
STATE_KEYS = {"version", "task_hash", "identity", "deadline", "attempts"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value):
    require(type(value) is dict and set(value) == {"mode", "directory", "host"}, "invalid identity")
    mode, directory, host = value["mode"], value["directory"], value["host"]
    require(type(mode) is str and mode in {"read", "write"}, "invalid mode")
    require(type(directory) is str and len(directory) <= 4096 and "\0" not in directory
            and os.path.isabs(directory), "directory must be absolute")
    require(host is None or (type(host) is str and 0 < len(host) <= 255 and "\0" not in host), "invalid host")
    return dict(value)


def process(value):
    require(type(value) is dict and set(value) <=
            {"local_status", "remote_status", "scope", "pipes_drained"}, "invalid process evidence")
    result = {"local_status": value.get("local_status", "unknown"),
              "remote_status": value.get("remote_status", "unknown"),
              "scope": value.get("scope", "unknown"), "pipes_drained": value.get("pipes_drained")}
    for name in ("local_status", "remote_status"):
        require(type(result[name]) is str and result[name] in STATUSES, "invalid process status")
    require(result["local_status"] != "not_applicable", "invalid local process status")
    require(type(result["scope"]) is str and result["scope"] in SCOPES, "invalid process scope")
    require(result["pipes_drained"] is None or type(result["pipes_drained"]) is bool, "invalid pipe evidence")
    # The scope records local process ownership, including the SSH client.
    # Missing evidence remains unknown; contradictory positive claims reject.
    if result["local_status"] == "stopped":
        require(result["scope"] == "owned_local_process_group" and
                result["pipes_drained"] is True, "contradictory stopped evidence")
    elif result["local_status"] == "not_started":
        require(result["scope"] in {"not_started", "unknown"} and
                result["pipes_drained"] is not False, "contradictory not_started evidence")
    elif result["local_status"] == "running":
        require(result["scope"] in {"owned_local_process_group", "unknown"},
                "contradictory running evidence")
    return result


def safe(evidence, host):
    local = evidence["local_status"]
    if local == "stopped":
        if evidence["scope"] != "owned_local_process_group" or evidence["pipes_drained"] is not True:
            return False
    elif local == "not_started":
        if evidence["scope"] != "not_started":
            return False
    else:
        return False
    if host is None:
        return evidence["remote_status"] in {"not_applicable", "not_started", "unknown"}
    if local == "not_started":
        return evidence["remote_status"] == "not_started"
    return evidence["remote_status"] in {"stopped", "not_started"}


def choice(attempts, host, tier, reason, contract_hash):
    require(type(tier) is str and tier in {"flash", "pro"}, "invalid tier")
    require(type(contract_hash) is str and HASH.fullmatch(contract_hash), "contract_hash must be SHA-256")
    require(reason is None or type(reason) is str, "invalid reason")
    if not attempts:
        require((tier == "flash" and reason is None) or (tier == "pro" and reason in
                {"explicit_request", "high_complexity"}), "invalid initial selection")
        return "initial"
    prior = attempts[-1]
    require(prior["phase"] == "ended", "unfinished attempt blocks continuation")
    require(safe(prior["process"], host), "stop evidence blocks continuation")
    failure = prior["classification"]
    if failure == "infrastructure":
        require(not any(a["kind"] == "infrastructure" for a in attempts), "infrastructure retry exhausted")
        require(tier == prior["tier"] and reason == prior["reason"] and
                contract_hash == prior["contract_hash"], "infrastructure retry must preserve selection and contract")
        return "infrastructure"
    require(failure == "substantive" and prior["tier"] == "flash", "failure is not eligible for continuation")
    require(tier == "pro" and reason == "flash_verification_failed", "only corrected Pro escalation is eligible")
    require(not any(a["tier"] == "pro" for a in attempts), "Pro substantive allowance exhausted")
    require(contract_hash != prior["contract_hash"], "corrected contract hash required")
    return "escalation"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


class Ledger:
    def __init__(self, directory, cleanup_reserve=2.0, minimum_execution=0.05):
        require(number(cleanup_reserve) and cleanup_reserve >= 0, "invalid cleanup reserve")
        require(number(minimum_execution) and minimum_execution > 0, "invalid minimum execution")
        self.directory = os.path.abspath(os.fspath(directory))
        self.reserve, self.minimum = float(cleanup_reserve), float(minimum_execution)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)

    def _read(self, path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES, "invalid ledger file")
            chunks, size = [], 0
            while size <= MAX_BYTES:
                chunk = os.read(fd, MAX_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        require(len(data) <= MAX_BYTES, "oversized ledger")
        try:
            parsed = json.loads(data, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("corrupt ledger JSON") from exc
        # None is reserved exclusively for ENOENT; JSON null is corrupt state.
        require(type(parsed) is dict, "invalid ledger top-level object")
        return parsed

    def _save(self, path, state):
        data = json.dumps(state, allow_nan=False, separators=(",", ":")).encode()
        require(len(data) <= MAX_BYTES, "ledger exceeds size limit")
        fd, temporary = tempfile.mkstemp(prefix=".ledger-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _validate(self, state, task_hash, expected_identity):
        require(type(state) is dict and set(state) == STATE_KEYS, "invalid ledger shape")
        require(type(state["version"]) is int and state["version"] == 1 and
                state["task_hash"] == task_hash, "invalid ledger identity")
        require(identity(state["identity"]) == expected_identity, "objective identity changed")
        require(number(state["deadline"]), "invalid deadline")
        attempts = state["attempts"]
        # Flash, its infrastructure retry, then Pro (or Flash, Pro, its retry).
        require(type(attempts) is list and len(attempts) <= 3, "invalid attempt history")
        history = []
        for index, attempt in enumerate(attempts):
            require(type(attempt) is dict and set(attempt) == ATTEMPT_KEYS, "invalid attempt shape")
            expected = choice(history, expected_identity["host"], attempt["tier"],
                              attempt["reason"], attempt["contract_hash"])
            require(attempt["kind"] == expected, "invalid retry history")
            evidence = process(attempt["process"])
            require(evidence == attempt["process"], "incomplete stored evidence")
            require(number(attempt["remaining_budget"]), "invalid stored budget")
            require(type(attempt["phase"]) is str and attempt["phase"] in {"active", "ended"}, "invalid phase")
            if attempt["phase"] == "active":
                require(index == len(attempts) - 1 and attempt["outcome"] == "unknown" and
                        attempt["classification"] == "unknown" and evidence == process({}), "invalid active reservation")
            else:
                classification, outcome = attempt["classification"], attempt["outcome"]
                require(type(classification) is str and classification in PAIRS and
                        type(outcome) is str and outcome in PAIRS[classification], "invalid completion")
            history.append(attempt)

    @contextmanager
    def locked(self, task_id, expected_identity, timeout_seconds):
        require(type(task_id) is str and 0 < len(task_id) <= 4096, "invalid task_id")
        expected_identity = identity(expected_identity)
        require(number(timeout_seconds) and timeout_seconds > 0, "invalid timeout")
        task_hash = hashlib.sha256(task_id.encode()).hexdigest()
        path = os.path.join(self.directory, task_hash + ".json")
        fd = os.open(os.path.join(self.directory, task_hash + ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
        session = None
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), "invalid lock file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    raise ValueError("objective busy") from exc
                raise
            state = self._read(path)
            if state is None:
                deadline = time.time() + timeout_seconds
                require(number(deadline), "invalid computed deadline")
                state = {"version": 1, "task_hash": task_hash, "identity": expected_identity,
                         "deadline": deadline, "attempts": []}
                self._save(path, state)
            self._validate(state, task_hash, expected_identity)
            session = _Session(self, path, state)
            yield session
        finally:
            if session is not None:
                session.open = False
            os.close(fd)


class _Session:
    def __init__(self, ledger, path, state):
        self.ledger, self.path, self.state = ledger, path, state
        self.open, self.launched, self.poisoned = True, False, False
        remaining = state["deadline"] - time.time()
        if state["attempts"]:
            remaining = min(remaining, state["attempts"][-1]["remaining_budget"])
        self.monotonic_deadline = time.monotonic() + remaining
        self.last_remaining = remaining

    def _check(self):
        require(self.open and not self.poisoned, "session unavailable")

    def _commit(self, candidate):
        try:
            self.ledger._save(self.path, candidate)
        except BaseException:
            self.poisoned = True
            raise
        self.state = candidate

    def remaining(self):
        self._check()
        now = time.monotonic()
        remaining = min(self.last_remaining, self.state["deadline"] - time.time(),
                        self.monotonic_deadline - now)
        # Retain every observed reduction, including a forward wall-clock jump.
        # A later rollback must not restore previously consumed budget.
        self.monotonic_deadline = now + remaining
        self.last_remaining = remaining
        return remaining

    def before_launch(self, tier="flash", reason=None, contract_hash=None):
        self._check()
        require(not self.launched, "one launch per locked session")
        kind = choice(self.state["attempts"], self.state["identity"]["host"], tier, reason, contract_hash)
        budget = self.remaining()
        require(budget > self.ledger.reserve + self.ledger.minimum, "insufficient remaining budget")
        candidate = copy.deepcopy(self.state)
        candidate["attempts"].append({"tier": tier, "reason": reason, "contract_hash": contract_hash,
             "kind": kind, "phase": "active", "outcome": "unknown", "classification": "unknown",
             "process": process({}), "remaining_budget": budget})
        self._commit(candidate)
        self.launched = True

    def finish(self, result, classification):
        self._check()
        require(self.launched and self.state["attempts"][-1]["phase"] == "active", "no current attempt")
        require(type(result) is dict, "invalid result")
        outcome = result.get("outcome")
        require(type(classification) is str and classification in PAIRS and
                type(outcome) is str and outcome in PAIRS[classification], "invalid classification")
        evidence = process(result.get("process", {}))
        candidate = copy.deepcopy(self.state)
        candidate["attempts"][-1].update(phase="ended", outcome=outcome,
            classification=classification, process=evidence, remaining_budget=self.remaining())
        self._commit(candidate)

    def snapshot(self):
        self._check()
        result = copy.deepcopy(self.state)
        result["usage"] = "unknown"
        return result
