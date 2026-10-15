"""通常のEthernet SCPI。Tcl ServerのRESULT応答は使用しない。"""
import logging
import math
import socket
import threading
import time

LOG = logging.getLogger(__name__)
COMMAND_LOG = logging.getLogger("md1230b.commands")
COMMAND_LOG.addHandler(logging.NullHandler())


class AnalyzerError(RuntimeError):
    pass


class TransportError(AnalyzerError):
    pass


class DeviceError(AnalyzerError):
    pass


class CleanupError(AnalyzerError):
    pass


class ScpiTransport:
    def __init__(self, host, tcp_port=5000, timeout=5.0, command_gap=0.05):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if not math.isfinite(command_gap) or command_gap < 0:
            raise ValueError("command_gap must be nonnegative and finite")
        self.host, self.tcp_port = host, tcp_port
        self.timeout, self.command_gap = timeout, command_gap
        self.lock = threading.RLock()
        self.usable = False
        self.sock = None
        self.last_send = 0.0

    def open(self):
        self.sock = socket.create_connection((self.host, self.tcp_port), self.timeout)
        self.usable = True

    def close(self):
        self.usable = False
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _send(self, command):
        if not self.usable:
            raise TransportError("接続が未接続、切断済み、または応答の対応関係が不明です")
        if any(c in command for c in "\r\n;"):
            raise ValueError("1コマンドずつ送信してください")
        data = (command + "\n").encode("ascii")
        time.sleep(max(0, self.command_gap - (time.monotonic() - self.last_send)))
        try:
            self.sock.settimeout(self.timeout)
            self.sock.sendall(data)
            self.last_send = time.monotonic()
        except BaseException as exc:
            self.usable = False
            # sendall失敗時は一部だけ届いた可能性があるため、送信済みと区別する。
            COMMAND_LOG.info("TX_FAILED %s:%s %s (%s)", self.host, self.tcp_port, command, exc)
            raise
        COMMAND_LOG.info("TX %s:%s %s", self.host, self.tcp_port, command)

    def query(self, command):
        if "?" not in command:
            raise ValueError("queryには問い合わせを指定してください")
        with self.lock:
            self._send(command)
            deadline = time.monotonic() + self.timeout
            buffer = bytearray()
            try:
                while b"\n" not in buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(command)
                    self.sock.settimeout(remaining)
                    part = self.sock.recv(8192)
                    if not part:
                        raise ConnectionError("装置が接続を切断しました")
                    buffer.extend(part)
                    if len(buffer) > 300000:
                        raise TransportError("応答長上限超過。終端文字/応答形式を確認してください")
                line, _, rest = buffer.partition(b"\n")
                if rest:
                    raise TransportError("問い合わせ1件に対し追加データを受信しました")
                answer = line.rstrip(b"\r").decode("ascii").strip()
                LOG.debug("< %s", answer[:180])
                return answer
            except BaseException:
                # 遅延応答を後続queryの応答と取り違えない。自動リトライもしない。
                self.usable = False
                raise

    def errors(self):
        with self.lock:
            errors = []
            for _ in range(32):
                answer = self.query(":SYSTem:ERRor?")
                try:
                    code = int(answer.split(",", 1)[0])
                except ValueError as exc:
                    self.usable = False
                    raise TransportError(f"不正なエラー応答: {answer!r}") from exc
                if code == 0:
                    return errors
                errors.append(answer)
            raise DeviceError(f"エラーキューが空になりません: {errors}")

    def check_errors(self, label):
        errors = self.errors()
        if errors:
            raise DeviceError(f"{label[:180]}: {'; '.join(errors)}")

    def write(self, command):
        if "?" in command:
            raise ValueError("問い合わせにはqueryを使用してください")
        with self.lock:
            self._send(command)
            self.check_errors(command)

    def query_checked(self, command):
        with self.lock:
            answer = self.query(command)
            self.check_errors(command)
            return answer
