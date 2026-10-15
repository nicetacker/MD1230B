import threading
import unittest
from unittest.mock import patch

from md1230b import AnalyzerError, CleanupError, EthernetFrame, Md1230b, PortAddress
from md1230b.transport import DeviceError


FRAME = EthernetFrame("02:00:00:00:00:01", "02:00:00:00:00:02", 0x88B5, b"test-id")
PORTS = {"PortA": PortAddress(1, 2, 1), "PortB": PortAddress(1, 2, 2)}


class FakeTransport:
    """装置の対象選択・予約・動作状態を持つ最小の模擬装置。"""
    def __init__(self, busy=(), fail_start=None, disconnect_start=None, frames=1):
        self.lock = threading.RLock()
        self.usable = False
        self.closed = False
        self.selected = [0, 0, 0]
        self.owned = set(busy)
        self.cap = {1: 0, 2: 0}
        self.tx = {1: 0, 2: 0}
        self.settings = {}
        self.log = []
        self.fail_start = fail_start
        self.disconnect_start = disconnect_start
        self.frames = frames

    def open(self): self.usable = True
    def close(self): self.closed = True; self.usable = False
    def errors(self): return []

    def _record(self, cmd):
        if not self.usable:
            raise ConnectionError("mock disconnected")
        self.log.append((tuple(self.selected), cmd))

    def write(self, cmd):
        self._record(cmd)
        p = self.selected[2]
        for index, prefix in enumerate((":UENTry:ID ", ":MODule:ID ", ":PORT:ID ")):
            if cmd.startswith(prefix):
                self.selected[index] = int(cmd.split()[-1])
                return
        if cmd == ":PORT:OWNership:TAKE": self.owned.add(p)
        elif cmd == ":PORT:OWNership:CLEar": self.owned.remove(p)
        elif cmd == ":CAPTure:STARt":
            if p == self.disconnect_start:
                self.usable = False
                raise TimeoutError("mock capture start timeout")
            if p == self.fail_start:
                raise DeviceError("mock capture start error")
            self.cap[p] = 1
        elif cmd == ":CAPTure:STOP": self.cap[p] = 0
        elif cmd == ":TSTReam:STOP": self.tx[p] = 0
        elif cmd == ":TSTReam:STARt": self.tx[p] = 0  # 有限送信が完了済み
        elif " " in cmd:
            key, value = cmd.split(" ", 1)
            self.settings[p, key] = value

    def query_checked(self, cmd):
        self._record(cmd)
        p = self.selected[2]
        if cmd == "*IDN?": return "ANRITSU,MD1230B,MOCK,1"
        if cmd == ":PORT:OWNership:CUSE?": return "0" if p in self.owned else "1"
        if cmd == ":TSTReam:STATe?": return str(self.tx[p])
        if cmd == ":CAPTure:STATe?": return str(self.cap[p])
        if cmd == ":TSTReam:TABLe:NSTReams?": return "1"
        if cmd == ":CAPTure:BUFFer:NFRames?": return str(self.frames)
        if cmd.startswith(":CAPTure:BUFFer:FRAMe:"):
            if any(self.cap.values()):
                raise AssertionError("全対象を止める前に読み出しています")
            if ":DATA?" in cmd: return "#H" + (FRAME.prefix() + bytes(10)).hex()
            if ":LENGth?" in cmd: return "128"
            if ":TIMestamp?" in cmd: return "12345.000000001"
        if cmd.endswith("?") and (p, cmd[:-1]) in self.settings:
            return self.settings[p, cmd[:-1]]
        raise AssertionError(f"Unexpected query: {cmd}")


class DriverTests(unittest.TestCase):
    def session(self, fake):
        return Md1230b("unused", PORTS, transport=fake, state_timeout=0.1)

    def test_two_ports_and_repeated_scenarios(self):
        fake = FakeTransport()
        with self.session(fake) as lan:
            lan["PortA"].configure_tx(FRAME, count=10, replace_streams=True)
            for _ in range(2):
                with lan.capture("PortA", "PortB") as cap:
                    self.assertEqual(fake.cap, {1: 1, 2: 1})
                    lan["PortA"].start_tx()
                    lan["PortA"].wait_tx_stopped()
                self.assertEqual(cap.results["PortB"].count(FRAME), 1)
                self.assertEqual(fake.owned, {1, 2})
                self.assertFalse(cap.results["PortB"].loss_checked)
            starts = [(p, cmd) for p, cmd in fake.log if cmd == ":TSTReam:STARt"]
            self.assertTrue(all(p == (1, 2, 1) for p, _ in starts))
        self.assertEqual(fake.owned, set())
        self.assertTrue(fake.closed)

    def test_partial_reservation_rolls_back_only_own_port(self):
        fake = FakeTransport(busy={2})
        with self.assertRaises(AnalyzerError):
            with self.session(fake): pass
        self.assertEqual(fake.owned, {2})
        cleared = [p[2] for p, cmd in fake.log if cmd == ":PORT:OWNership:CLEar"]
        self.assertEqual(cleared, [1])

    def test_partial_capture_start_stops_first_port(self):
        fake = FakeTransport(fail_start=2)
        with self.session(fake) as lan:
            with self.assertRaises(DeviceError):
                with lan.capture(): pass
            self.assertEqual(fake.cap, {1: 0, 2: 0})
        self.assertFalse(fake.owned)

    def test_scenario_failure_preserves_exception_and_capture(self):
        fake = FakeTransport()
        with self.session(fake) as lan:
            cap = lan.capture()
            with self.assertRaisesRegex(AssertionError, "device failure"):
                with cap:
                    raise AssertionError("device failure")
            self.assertEqual(set(cap.results), {"PortA", "PortB"})
            self.assertEqual(fake.cap, {1: 0, 2: 0})
        self.assertFalse(fake.owned)

    def test_capture_limit_is_error_not_silent_truncation(self):
        fake = FakeTransport(frames=2)
        with self.session(fake) as lan:
            cap = lan.capture(max_frames=1)
            with self.assertRaises(CleanupError), self.assertLogs("md1230b.driver", level="ERROR"):
                with cap: pass
            self.assertEqual(cap.results, {})
            self.assertEqual(len(cap.errors), 2)

    def test_disconnect_never_claims_release_or_hides_original(self):
        fake = FakeTransport(disconnect_start=2)
        lan = self.session(fake)
        with self.assertRaisesRegex(TimeoutError, "capture start timeout"), self.assertLogs("md1230b.driver", level="ERROR"):
            with lan:
                with lan.capture(): pass
        self.assertTrue(fake.closed)
        self.assertTrue(lan.cleanup_errors)
        self.assertEqual(fake.owned, {1, 2})

    def test_failed_tx_stop_blocks_other_port_until_cleanup(self):
        for operation in ("stop_tx", "wait_tx_stopped", "configure_tx"):
            for failure in ("timeout", "query_error", "invalid_state"):
                with self.subTest(operation=operation, failure=failure):
                    fake = FakeTransport()
                    with self.session(fake) as lan:
                        lan["PortB"].configure_tx(FRAME, replace_streams=True)
                        lan.state_timeout = 0.001
                        query = fake.query_checked

                        def failed_state(cmd):
                            if fake.selected[2] == 1 and cmd == ":TSTReam:STATe?":
                                if failure == "query_error":
                                    raise DeviceError("mock Tx state error")
                                return "invalid" if failure == "invalid_state" else "1"
                            return query(cmd)

                        with patch.object(fake, "query_checked", side_effect=failed_state):
                            with self.assertRaises((AnalyzerError, ValueError)):
                                if operation == "configure_tx":
                                    lan["PortA"].configure_tx(FRAME, replace_streams=True)
                                else:
                                    getattr(lan["PortA"], operation)()
                        before = len(fake.log)
                        with self.assertRaises(AnalyzerError):
                            lan["PortB"].start_tx()
                        with self.assertRaises(AnalyzerError):
                            lan["PortB"].configure_tx(FRAME, replace_streams=True)
                        self.assertEqual(len(fake.log), before)
                    self.assertFalse(fake.owned)
                    self.assertTrue(fake.closed)

    def test_rejected_tx_stop_blocks_other_port(self):
        fake = FakeTransport()
        with self.session(fake) as lan:
            lan["PortB"].configure_tx(FRAME, replace_streams=True)
            write = fake.write

            def rejected_stop(cmd):
                if fake.selected[2] == 1 and cmd == ":TSTReam:STOP":
                    raise DeviceError("mock Tx stop error")
                return write(cmd)

            with patch.object(fake, "write", side_effect=rejected_stop):
                with self.assertRaises(DeviceError):
                    lan["PortA"].stop_tx()
            with self.assertRaises(AnalyzerError):
                lan["PortB"].start_tx()
        self.assertFalse(fake.owned)

    def test_capture_state_error_still_stops_all_ports(self):
        for original_failure in (False, True):
            with self.subTest(original_failure=original_failure):
                fake = FakeTransport()
                with self.session(fake) as lan:
                    cap = lan.capture()
                    query = fake.query_checked
                    fail_state = False

                    def failed_state_once(cmd):
                        nonlocal fail_state
                        if fail_state and fake.selected[2] == 1 and cmd == ":CAPTure:STATe?":
                            fail_state = False
                            raise DeviceError("mock capture state error")
                        return query(cmd)

                    expected = AssertionError if original_failure else CleanupError
                    with patch.object(fake, "query_checked", side_effect=failed_state_once):
                        with self.assertRaises(expected) as caught, self.assertLogs("md1230b.driver", level="ERROR"):
                            with cap:
                                fail_state = True
                                if original_failure:
                                    raise AssertionError("original scenario failure")
                    self.assertEqual(fake.cap, {1: 0, 2: 0})
                    self.assertEqual(set(cap.results), {"PortB"})
                    self.assertTrue(any("mock capture state error" in error for error in cap.errors))
                    if original_failure:
                        self.assertEqual(str(caught.exception), "original scenario failure")
                self.assertFalse(fake.owned)

    def test_capture_stop_failure_skips_all_reads_and_attempts_other_port(self):
        fake = FakeTransport()
        with self.session(fake) as lan:
            cap = lan.capture()
            write = fake.write
            fail_stop = False

            def rejected_stop(cmd):
                if fail_stop and fake.selected[2] == 1 and cmd == ":CAPTure:STOP":
                    raise DeviceError("mock capture stop error")
                return write(cmd)

            with patch.object(fake, "write", side_effect=rejected_stop):
                with self.assertRaises(CleanupError), self.assertLogs("md1230b.driver", level="ERROR"):
                    with cap:
                        fake.log.clear()
                        fail_stop = True
            self.assertEqual(fake.cap, {1: 1, 2: 0})
            self.assertEqual(cap.results, {})
            self.assertFalse(any(":CAPTure:BUFFer:" in cmd for _, cmd in fake.log))
            with self.assertRaises(AnalyzerError):
                lan["PortA"].stop_tx()
        self.assertEqual(fake.cap, {1: 0, 2: 0})
        self.assertFalse(fake.owned)


if __name__ == "__main__":
    unittest.main()
