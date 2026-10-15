import socket
import threading
import time
import unittest
from unittest.mock import Mock

from md1230b.transport import ScpiTransport, TransportError


class TransportTests(unittest.TestCase):
    def transport_pair(self, timeout=0.2):
        client, server = socket.socketpair()
        io = ScpiTransport("unused", timeout=timeout, command_gap=0)
        io.sock, io.usable = client, True
        self.addCleanup(io.close)
        self.addCleanup(server.close)
        return io, server

    def test_fragmented_crlf_response(self):
        io, server = self.transport_pair()
        def reply():
            server.recv(1024)
            for part in (b"#H0011", b"2233", b"\r", b"\n"):
                server.sendall(part)
                time.sleep(0.005)
        worker = threading.Thread(target=reply)
        worker.start()
        self.assertEqual(io.query(":CAPTure:BUFFer:FRAMe:DATA? 1"), "#H00112233")
        worker.join()

    def test_command_history_records_full_commands_queries_and_error_checks(self):
        io, server = self.transport_pair()
        command = ":TSTReam:DATA " + "AB" * 200
        expected = [command, ":SYSTem:ERRor?", "*IDN?", ":SYSTem:ERRor?"]
        received = []

        def reply():
            with server.makefile("rb") as reader:
                for answer in (None, b'0,"No error"\n', b"ANRITSU,MD1230B\n", b'0,"No error"\n'):
                    received.append(reader.readline().decode("ascii").rstrip("\n"))
                    if answer is not None:
                        server.sendall(answer)

        worker = threading.Thread(target=reply)
        worker.start()
        with self.assertLogs("md1230b.commands", level="INFO") as history:
            io.write(command)
            self.assertEqual(io.query_checked("*IDN?"), "ANRITSU,MD1230B")
        worker.join()
        self.assertEqual(received, expected)
        self.assertEqual([record.getMessage() for record in history.records],
                         [f"TX unused:5000 {cmd}" for cmd in expected])

    def test_failed_send_is_distinguished_and_connection_cannot_be_reused(self):
        io = ScpiTransport("unused", command_gap=0)
        io.sock = Mock()
        io.sock.sendall.side_effect = OSError("mock send failure")
        io.usable = True
        self.addCleanup(io.close)
        with self.assertLogs("md1230b.commands", level="INFO") as history:
            with self.assertRaisesRegex(OSError, "mock send failure"):
                io.query("*IDN?")
            with self.assertRaises(TransportError):
                io.query("*IDN?")
        self.assertFalse(io.usable)
        self.assertEqual([record.getMessage() for record in history.records],
                         ["TX_FAILED unused:5000 *IDN? (mock send failure)"])

    def test_timeout_rejects_late_response_and_reuse(self):
        io, server = self.transport_pair(timeout=0.02)
        with self.assertRaises(TimeoutError):
            io.query("*IDN?")
        self.assertFalse(io.usable)
        server.sendall(b"late reply\n")
        with self.assertRaises(TransportError):
            io.query(":SYSTem:ERRor?")

    def test_query_deadline_not_reset_by_partial_bytes(self):
        io, server = self.transport_pair(timeout=0.04)
        def reply():
            server.recv(1024)
            for _ in range(4):
                server.sendall(b"x")
                time.sleep(0.02)
        worker = threading.Thread(target=reply)
        worker.start()
        with self.assertRaises(TimeoutError):
            io.query("*IDN?")
        worker.join()
        self.assertFalse(io.usable)


if __name__ == "__main__":
    unittest.main()
