"""MD1230Bポート予約・有限送信・キャプチャの同期API。"""
from contextlib import contextmanager
from decimal import Decimal
import logging
import math
import time

from .models import CaptureResult, CapturedFrame, EthernetFrame, PortAddress, mac_bytes
from .transport import AnalyzerError, CleanupError, DeviceError, ScpiTransport

LOG = logging.getLogger(__name__)


def _report_cleanup(original, errors):
    if not errors:
        return
    message = "後処理失敗: " + "; ".join(errors)
    LOG.error(message)
    if original is not None:
        if hasattr(original, "add_note"):
            original.add_note(message)
    else:
        raise CleanupError(message)


class Md1230b:
    """with開始時に全ポートを予約。終了時に停止・解放し接続を閉じる。

    一つのセッションで同時に実行できる試験は一つ。
    同一装置の対象選択〜コマンド処理をRLockで保護する。
    GUIや別プロセスによる同時操作の調停は行わない。
    """

    def __init__(self, host: str, ports: dict[str, PortAddress], *, tcp_port=5000,
                 timeout=5.0, command_gap=0.05, state_timeout=10.0, transport=None):
        if not ports or len(set(ports.values())) != len(ports):
            raise ValueError("portsは空でなく、異なる物理ポートを割り当ててください")
        if not math.isfinite(state_timeout) or state_timeout <= 0:
            raise ValueError("state_timeout must be positive and finite")
        self.ports = dict(ports)
        self.state_timeout = state_timeout
        self.io = transport or ScpiTransport(host, tcp_port, timeout, command_gap)
        self._owned = []
        self._uncertain = set()
        self._active = False
        self._used = False
        self._capture_active = False
        self._tainted = False
        self._prepared = set()
        self.cleanup_errors = []
        self.identity = ""

    def _select(self, name):
        p = self.ports[name]
        for command in (f":UENTry:ID {p.unit}", f":MODule:ID {p.module}", f":PORT:ID {p.port}"):
            self.io.write(command)

    @contextmanager
    def _at(self, name, *, recovery=False):
        with self.io.lock:
            if name not in self._owned:
                raise AnalyzerError(f"予約していないポートです: {name}")
            if not recovery and (not self._active or self._tainted):
                raise AnalyzerError("セッションが使用できません。後処理結果を確認してください")
            self._select(name)
            yield

    def _wait(self, query, states):
        deadline = time.monotonic() + self.state_timeout
        while True:
            state = int(self.io.query_checked(query))
            if state in states:
                return state
            if time.monotonic() >= deadline:
                raise AnalyzerError(f"状態待ちタイムアウト: {query}, state={state}")
            time.sleep(0.1)

    def _wait_stopped(self, subsystem, *, stop=False):
        """選択済みポートの停止を確認。失敗後は後処理だけを許可する。"""
        try:
            if stop:
                self.io.write(f"{subsystem}:STOP")
            self._wait(f"{subsystem}:STATe?", {0})
        except BaseException:
            self._tainted = True
            raise

    def __enter__(self):
        if self._used:
            raise AnalyzerError("セッションオブジェクトは再利用せず作り直してください")
        self._used = True
        try:
            self.io.open()
            stale = self.io.errors()
            if stale:
                LOG.warning("接続時の既存エラー: %s", stale)
            self.identity = self.io.query_checked("*IDN?")
            with self.io.lock:
                for name in self.ports:
                    self._select(name)
                    if int(self.io.query_checked(":PORT:OWNership:CUSE?")) != 1:
                        raise AnalyzerError(f"ポートが使用中です: {name}")
                    self._uncertain.add(name)
                    try:
                        self.io.write(":PORT:OWNership:TAKE")
                    except DeviceError:
                        self._uncertain.discard(name)
                        raise
                    self._owned.append(name)
                    self._uncertain.discard(name)
            self._active = True
            return self
        except BaseException as exc:
            self.close(original=exc)
            raise

    def __getitem__(self, name):
        if name not in self.ports:
            raise KeyError(name)
        return Port(self, name)

    def capture(self, *names, max_frames=1000):
        return Capture(self, names or tuple(self.ports), max_frames)

    def close(self, *, original=None):
        errors = []
        try:
            for name in reversed(self._owned):
                for subsystem in (":TSTReam", ":CAPTure"):
                    try:
                        with self._at(name, recovery=True):
                            self._wait_stopped(subsystem, stop=True)
                    except Exception as exc:
                        errors.append(f"{name} {subsystem}停止未確認: {exc}")
                try:
                    with self._at(name, recovery=True):
                        self.io.write(":PORT:OWNership:CLEar")
                        if int(self.io.query_checked(":PORT:OWNership:CUSE?")) != 1:
                            raise AnalyzerError("解放後もVacantではありません")
                except Exception as exc:
                    errors.append(f"{name} 所有権解放未確認: {exc}")
            errors.extend(f"{name} TAKEの結果が不明です。実機で所有権を確認してください" for name in self._uncertain)
        finally:
            self._active = False
            self._owned.clear()
            self._uncertain.clear()
            self.cleanup_errors.extend(errors)
            self.io.close()
        _report_cleanup(original, errors)

    def __exit__(self, exc_type, exc, traceback):
        self.close(original=exc)
        return False


class Port:
    def __init__(self, analyzer, name):
        self.analyzer, self.name = analyzer, name

    def configure_tx(self, frame: EthernetFrame, *, count=1, replace_streams=False):
        """指定フレームをcount個送る1ストリームを準備。既存設定は復元しない。"""
        if not isinstance(frame, EthernetFrame):
            raise TypeError("frameにはEthernetFrameを指定してください")
        if type(count) is not int or not 1 <= count <= 16777215:
            raise ValueError("countは1..16777215を指定してください")
        a = self.analyzer
        with a._at(self.name):
            a._prepared.discard(self.name)
            a._wait_stopped(":TSTReam", stop=True)
            n = int(a.io.query_checked(":TSTReam:TABLe:NSTReams?"))
            if n and not replace_streams:
                raise AnalyzerError("既存ストリームがあります。置換する場合はreplace_streams=Trueを指定してください")
            if n:
                a.io.write(":TSTReam:TABLe:ACLear")
            a.io.write(":TSTReam:TABLe:ADD")
            a.io.write(":TSTReam:TABLe:ID 1")
            b = ":TSTReam:TABLe:ITEM"
            settings = [
                ("ENABle", "ON"),
                ("CONTrol:DISTribution", "STOP"),
                ("CONTrol:FPBurst", str(count)),
                ("CONTrol:BPSTream", "1"),
                ("PROTocol:TYPE", "NONE"),
                ("FRAMe:VLAN:ENABle", "OFF"),
                ("FRAMe:MPLS:ENABle", "OFF"),
                ("FRAMe:ETHernet:SA:TYPE", "STATIC"),
                ("FRAMe:ETHernet:DA:TYPE", "STATIC"),
                ("FRAMe:ETHernet:SA:VALue", "#H" + mac_bytes(frame.sa).hex()),
                ("FRAMe:ETHernet:DA:VALue", "#H" + mac_bytes(frame.da).hex()),
                ("FRAMe:ETHernet:PROTocol:AUTO", "OFF"),
                ("FRAMe:ETHernet:TYPE", f"#H{frame.ethertype:04X}"),
                ("FSIZe:TYPE", "FIXED"),
                ("FSIZe:VALue", str(frame.frame_length)),
                ("FRAMe:BDATa", "ALL0"),
                ("FRAMe:DFIeld:HEADer:DATA", "0"),
                ("FRAMe:DFIeld2:ENABle", "OFF"),
                ("FRAMe:DFIeld3:ENABle", "OFF"),
                ("FRAMe:DFIeld4:ENABle", "OFF"),
                ("FRAMe:DFIeld1:ENABle", "ON" if frame.payload else "OFF"),
            ]
            if frame.payload:
                settings.extend([
                    ("FRAMe:DFIeld1:OFFSet", "0"),
                    ("FRAMe:DFIeld1:TYPE", "PROGRAM"),
                    ("FRAMe:DFIeld1:LENGth", str(len(frame.payload))),
                    ("FRAMe:DFIeld1:PROGram:PATTern", f"{len(frame.payload)},#H{frame.payload.hex()}"),
                ])
            for key, value in settings:
                a.io.write(f"{b}:{key} {value}")
            a.io.write(":TSTReam:TABLe:WRITe")
            for key, wanted in (("FSIZe:VALue", frame.frame_length), ("CONTrol:FPBurst", count), ("CONTrol:BPSTream", 1)):
                actual = int(a.io.query_checked(f"{b}:{key}?"))
                if actual != wanted:
                    raise AnalyzerError(f"設定読み戻し不一致: {key}: {actual} != {wanted}")
            a._prepared.add(self.name)

    def start_tx(self):
        a = self.analyzer
        with a._at(self.name):
            if self.name not in a._prepared:
                raise AnalyzerError("configure_txを先に呼んでください")
            if int(a.io.query_checked(":TSTReam:STATe?")) != 0:
                raise AnalyzerError("既に送信中です")
            a.io.write(":TSTReam:STARt")

    def wait_tx_stopped(self):
        """有限送信の停止待ち。実送信数の保証には別途Txカウンターも確認する。"""
        a = self.analyzer
        with a._at(self.name):
            a._wait_stopped(":TSTReam")

    def stop_tx(self):
        a = self.analyzer
        with a._at(self.name):
            a._wait_stopped(":TSTReam", stop=True)


class Capture:
    """with内で取得を継続し、終了時に全対象を止めてから読み出す。"""
    def __init__(self, analyzer, names, max_frames):
        if not names or len(set(names)) != len(names):
            raise ValueError("キャプチャ対象ポートが空または重複しています")
        if any(name not in analyzer.ports for name in names):
            raise KeyError("未定義のキャプチャポートです")
        if type(max_frames) is not int or max_frames < 1:
            raise ValueError("max_framesは正の整数です")
        self.analyzer, self.names, self.max_frames = analyzer, names, max_frames
        self.results = {}
        self.errors = []
        self._attempted = []
        self._used = False

    def __enter__(self):
        a = self.analyzer
        if self._used or a._capture_active:
            raise AnalyzerError("Captureの再利用/入れ子/同時実行はできません")
        self._used = True
        a._capture_active = True
        try:
            for name in self.names:
                with a._at(name):
                    self._attempted.append(name)
                    a._wait_stopped(":CAPTure", stop=True)
                    a.io.write(":CAPTure:FILTer:ENABle OFF")
                    a.io.write(":CAPTure:TRIGger:ENABle OFF")
                    a.io.write(":CAPTure:STARt")
                    a._wait(":CAPTure:STATe?", {1, 2})
            self.check_running()
            return self
        except BaseException as exc:
            self._finish(exc, fetch=False)
            raise

    def check_running(self):
        a = self.analyzer
        for name in self.names:
            with a._at(name):
                state = int(a.io.query_checked(":CAPTure:STATe?"))
                if state not in (1, 2):
                    raise AnalyzerError(f"{name}: キャプチャが動作中ではありません (state={state})")

    def observe(self, seconds):
        """受信待ち時間。試験対象装置の操作中でもcheck_runningを呼び出せる。"""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be nonnegative and finite")
        deadline = time.monotonic() + seconds
        while True:
            self.check_running()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))

    def _read(self, name, state):
        a = self.analyzer
        with a._at(name, recovery=True):
            count = int(a.io.query_checked(":CAPTure:BUFFer:NFRames?"))
            if count < 0 or count > self.max_frames:
                raise AnalyzerError(f"{name}: {count}フレーム。読出し上限{self.max_frames}超過/不正値。黙って切り捨てません")
            frames = []
            for index in range(1, count + 1):
                raw = a.io.query_checked(f":CAPTure:BUFFer:FRAMe:DATA? {index}")
                if not raw.upper().startswith("#H"):
                    raise AnalyzerError(f"不正なキャプチャ形式: {raw[:100]!r}")
                data = bytes.fromhex(raw[2:])
                length = int(a.io.query_checked(f":CAPTure:BUFFer:FRAMe:LENGth? {index}"))
                timestamp = Decimal(a.io.query_checked(f":CAPTure:BUFFer:FRAMe:TIMestamp? {index}"))
                frames.append(CapturedFrame(index, data, length, timestamp))
            return CaptureResult(name, tuple(frames), count, state)

    def _finish(self, original, *, fetch=True):
        a = self.analyzer
        errors, states, stopped = [], {}, []
        try:
            # 例外時にも、先に送信を止める。読出し中に別ポートを溢れさせない。
            for name in a._owned:
                try:
                    with a._at(name, recovery=True):
                        a._wait_stopped(":TSTReam", stop=True)
                except Exception as exc:
                    errors.append(f"{name} Tx停止失敗: {exc}")
                    a._tainted = True
            for name in self._attempted:
                try:
                    with a._at(name, recovery=True):
                        try:
                            state = int(a.io.query_checked(":CAPTure:STATe?"))
                            states[name] = state
                            if fetch and state not in (1, 2):
                                errors.append(f"{name} キャプチャが予定より早く停止/解析状態になりました: {state}")
                        except Exception as exc:
                            errors.append(f"{name} Capture状態取得失敗: {exc}")
                            a._tainted = True
                        # 状態の取得に失敗しても、利用可能な通信で停止を試みる。
                        a._wait_stopped(":CAPTure", stop=True)
                        stopped.append(name)
                except Exception as exc:
                    errors.append(f"{name} Capture停止失敗: {exc}")
                    a._tainted = True
            if fetch and len(stopped) != len(self._attempted):
                errors.append("全対象キャプチャの停止を確認できないため、読出しを中止しました")
            elif fetch:
                for name in stopped:
                    if name not in states:
                        continue  # 停止前の状態が不明なポートの結果は返さない。
                    try:
                        self.results[name] = self._read(name, states[name])
                    except Exception as exc:
                        errors.append(f"{name} 読出し失敗: {exc}")
        finally:
            a._capture_active = False
            self.errors.extend(errors)
        _report_cleanup(original, errors)

    def __exit__(self, exc_type, exc, traceback):
        self._finish(exc)
        return False
