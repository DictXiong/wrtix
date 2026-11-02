"""Best-effort stderr progress, with one coordinated animation per terminal."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import os
import re
import sys
import threading
import time
import unicodedata


@dataclass(eq=False)
class _Stage:
    owner: Progress
    label: str
    stream: object
    started: float
    animate: bool
    frames: str
    frame: int = 0
    rendered: bool = False
    external_shown: bool = False


class Progress:
    _output_lock = threading.Lock()
    _stages = []
    _pauses = {}
    _deferred = {}

    def __init__(self, program, quiet=False, device=None, *, interval=10.0, stream=None):
        self.program, self.quiet, self.device = program, quiet, device
        self.interval, self.stream = interval, stream
        self._disabled = False

    def for_device(self, device):
        return Progress(self.program, self.quiet, device, interval=self.interval, stream=self.stream)

    def _stream(self):
        return self.stream if self.stream is not None else sys.stderr

    def _prefix(self):
        prefix = self.program
        if self.device is not None:
            prefix += "[" + re.sub(r"[^A-Za-z0-9_.-]", "?", str(self.device)) + "]"
        return prefix + ": "

    def _write(self, stream, text):
        if self._disabled:
            return False
        try:
            stream.write(text)
            stream.flush()
            return True
        except (OSError, ValueError):
            # A closed log pipe cannot change a deployment's result or retries.
            self._disabled = True
            return False

    @classmethod
    def _active(cls, stream):
        return next((stage for stage in reversed(cls._stages) if stage.stream is stream), None)

    @classmethod
    def _clear(cls, stage):
        if stage is not None and stage.rendered:
            stage.owner._write(stage.stream, "\r\x1b[2K")
            stage.rendered = False

    @staticmethod
    def _fit(text, stream):
        try:
            columns = os.get_terminal_size(stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            columns = 80
        width = max(1, (columns or 80) - 1)
        cells = lambda char: 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if sum(cells(char) for char in text) <= width:
            return text
        suffix, used, result = ("..." if width >= 3 else ""), 0, []
        for char in text:
            used += cells(char)
            if used > width - len(suffix):
                break
            result.append(char)
        return "".join(result) + suffix

    @classmethod
    def _draw(cls, stage):
        if stage is None or not stage.animate or cls._pauses.get(id(stage.stream)):
            return
        elapsed = format(time.monotonic() - stage.started, ".1f")
        text = stage.owner._prefix() + stage.frames[stage.frame % len(stage.frames)] + " (" + elapsed + "s) " + stage.label
        stage.rendered = stage.owner._write(stage.stream, "\r\x1b[2K" + cls._fit(text, stage.stream))
        stage.frame += 1

    def _message_locked(self, text, stream):
        if self._pauses.get(id(stream)):
            self._deferred.setdefault(id(stream), []).append((self, text))
            return
        active = self._active(stream)
        self._clear(active)
        self._write(stream, self._prefix() + text + "\n")
        self._draw(active)

    def message(self, text):
        """Only pass fixed messages and validated public metadata, never RPC output."""
        if self.quiet or self._disabled:
            return
        text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        with self._output_lock:
            self._message_locked(text, self._stream())

    @classmethod
    @contextlib.contextmanager
    def external_output(cls, stream=None):
        """Pause progress while SSH or another tool owns the terminal prompt."""
        stream = stream if stream is not None else sys.stderr
        key = id(stream)
        with cls._output_lock:
            count = cls._pauses.get(key, 0)
            cls._pauses[key] = count + 1
            active = cls._active(stream)
            if count == 0 and active is not None and active.animate:
                cls._clear(active)
                if not active.external_shown:
                    active.owner._write(stream, active.owner._prefix() + "> " + active.label + "\n")
                    active.external_shown = True
        try:
            yield
        finally:
            with cls._output_lock:
                cls._pauses[key] -= 1
                if cls._pauses[key] == 0:
                    del cls._pauses[key]
                    active = cls._active(stream)
                    deferred = cls._deferred.pop(key, [])
                    if deferred:
                        # A message queued during an external prompt needs its own line.
                        deferred[0][0]._write(stream, "\n")
                    for owner, text in deferred:
                        owner._write(stream, owner._prefix() + text + "\n")
                    cls._draw(active)

    @staticmethod
    def _terminal_animation(stream, requested):
        try:
            return bool(requested and stream.isatty() and os.environ.get("TERM") != "dumb")
        except (AttributeError, OSError, ValueError):
            return False

    @staticmethod
    def _frames(stream):
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        try:
            frames.encode(getattr(stream, "encoding", None) or "ascii")
        except (LookupError, UnicodeError):
            frames = "|/-\\"
        return frames

    @contextlib.contextmanager
    def stage(self, label, *, animate=True):
        """Report duration; animate a TTY or emit bounded plain waiting lines."""
        if self.quiet or self._disabled:
            yield
            return
        label = re.sub(r"[\x00-\x1f\x7f]", " ", label)
        stream = self._stream()
        stage = _Stage(self, label, stream, time.monotonic(), self._terminal_animation(stream, animate), self._frames(stream))
        done = threading.Event()
        with self._output_lock:
            self._clear(self._active(stream))
            self._stages.append(stage)
            if stage.animate:
                self._draw(stage)
            else:
                self._message_locked("> " + label, stream)

        def heartbeat():
            while not done.wait(.1 if stage.animate else self.interval):
                with self._output_lock:
                    if self._active(stream) is not stage or self._pauses.get(id(stream)):
                        continue
                    if stage.animate:
                        self._draw(stage)
                    else:
                        elapsed = str(int(time.monotonic() - stage.started))
                        self._message_locked("... " + label + " (" + elapsed + "s elapsed; still running)", stream)

        worker = threading.Thread(target=heartbeat, name="openwrt-progress", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            worker = None
        outcome = "OK"
        try:
            yield
        except BaseException:
            outcome = "FAIL"
            raise
        finally:
            done.set()
            if worker is not None:
                worker.join()
            with self._output_lock:
                self._clear(self._active(stream))
                self._stages.remove(stage)
                self._message_locked(outcome + " " + label + " (" + format(time.monotonic() - stage.started, ".1f") + "s)", stream)
