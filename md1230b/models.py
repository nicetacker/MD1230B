"""装置に接続せず利用できるフレーム定義と取得結果。"""
from dataclasses import dataclass
from decimal import Decimal
import re


def mac_bytes(value: str) -> bytes:
    text = value.replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{12}", text):
        raise ValueError(f"MACアドレスが不正です: {value!r}")
    return bytes.fromhex(text)


@dataclass(frozen=True)
class PortAddress:
    unit: int
    module: int
    port: int

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.unit, self.module, self.port)):
            raise ValueError("Unit/Module/Portには1以上の実機IDを指定してください")


@dataclass(frozen=True)
class EthernetFrame:
    sa: str
    da: str
    ethertype: int
    payload: bytes = b""
    frame_length: int = 128

    def __post_init__(self):
        mac_bytes(self.sa)
        mac_bytes(self.da)
        if not isinstance(self.payload, bytes):
            raise TypeError("payloadにはbytesを指定してください")
        if type(self.ethertype) is not int or not 0x0600 <= self.ethertype <= 0xFFFF:
            raise ValueError("このサンプルはEtherType 0x0600..0xFFFFに対応します")
        if type(self.frame_length) is not int or self.frame_length < 64:
            raise ValueError("このサンプルはframe_length >= 64に対応します")
        # FSIZEに渡す生値。14byteヘッダ+4byte FCS分を保守的に確保する。
        if len(self.payload) > min(65535, self.frame_length - 18):
            raise ValueError("payloadが長すぎます。ヘッダ/FCS用に18byte確保してください")

    def prefix(self) -> bytes:
        """DA〜payload。パディング/FCSを含まない比較用の先頭部分。"""
        return mac_bytes(self.da) + mac_bytes(self.sa) + self.ethertype.to_bytes(2, "big") + self.payload


@dataclass(frozen=True)
class CapturedFrame:
    index: int
    data: bytes
    reported_length: int
    timestamp_utc_seconds: Decimal


@dataclass(frozen=True)
class CaptureResult:
    port_name: str
    frames: tuple[CapturedFrame, ...]
    reported_count: int
    state_before_stop: int
    # 全バッファを読み出しても、装置の取りこぼしなしとは証明できない。
    loss_checked: bool = False

    def matching(self, frame: EthernetFrame) -> tuple[CapturedFrame, ...]:
        prefix = frame.prefix()
        return tuple(item for item in self.frames if item.data.startswith(prefix))

    def count(self, frame: EthernetFrame) -> int:
        return len(self.matching(frame))
