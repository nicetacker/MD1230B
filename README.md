# MD1230B 汎用2ポート試験用Pythonモジュール

通常のEthernet SCPI (TCPポート5000) 用です。Tcl Serverは使用しません。
Python 3.9以上、標準ライブラリのみ。**実機未検証の実装サンプル**です。
モデル名はMD1230Bを前提としています。

## 組み込み

`md1230b/` ディレクトリをテストツールのimport可能な場所に配置します。
`example.py` は既存のテストツールから呼ぶシナリオ関数の例です。

```python
from md1230b import Md1230b, PortAddress, EthernetFrame

ports = {
    "PortA": PortAddress(unit=1, module=2, port=1),  # 試験ポートA
    "PortB": PortAddress(unit=1, module=2, port=2),  # 試験ポートB
}

# 一連の回帰試験の外側。両ポートを予約する。
with Md1230b("192.168.0.100", ports) as lan:
    frame = EthernetFrame(
        sa="02:00:00:00:00:01", da="02:00:00:00:00:02",
        ethertype=0x88B5, payload=b"case001-unique-run-id", frame_length=128,
    )
    lan["PortA"].configure_tx(frame, count=10, replace_streams=True)

    # シナリオごとのキャプチャ。with内では両ポートが取得中。
    with lan.capture("PortA", "PortB", max_frames=1000) as cap:
        # ここで既存ツールの試験対象装置コマンド送信・応答確認を呼べる。
        lan["PortA"].start_tx()
        lan["PortA"].wait_tx_stopped()
        cap.observe(0.5)

    # キャプチャ停止・解析完了・読出し後。判定は呼出側に置く。
    actual = cap.results["PortB"].count(frame)
    if actual != 10:
        raise AssertionError(f"PortA→PortB受信数: {actual}, expected=10")
# 送信/キャプチャ停止、所有権解放、TCP切断。
```

`example.py` の実行例:

```bash
python example.py 192.168.0.100 --unit 1 --module 2 --port-a 1 --port-b 2 --verbose
```

**この例は対象ポートの既存Tx Stream設定を削除し、PortA→PortB・PortB→PortAに各10フレーム送信します。**
GUIでリンク速度などを設定済みの、専用の試験ポートで使ってください。

ポート名は任意の文字列を使えます。この例では `PortA` / `PortB` に統一し、
CLIでは `--port-a` / `--port-b` で物理ポート番号を指定します。
既存ツールの操作は `bidirectional_test(lan, device_action=制御関数)` で差し込めます。

## 送信コマンドの履歴

標準出力で確認する場合:

```bash
python example.py 192.168.0.100 --unit 1 --module 2 --port-a 1 --port-b 2 --command-log -
```

UTF-8テキストファイルに追記する場合:

```bash
python example.py 192.168.0.100 --unit 1 --module 2 --port-a 1 --port-b 2 --command-log commands.log
```

日時・接続先・コマンド全文を送信順に記録します。ポート選択、設定、問い合わせ、
エラー確認の `:SYSTem:ERRor?`、終了時の停止・解放も含み、長いコマンドも省略しません。
`TX` はTCPの送信処理が成功した記録で、装置での実行成功を保証するものではありません。
`TX_FAILED` は送信処理の失敗です。一部だけ装置に届いた可能性があります。
`--verbose` では送信コマンドと受信応答のデバッグログを標準出力に表示します
（受信応答は先頭180文字）。`--command-log` と併用するとコマンド履歴は指定先に出力します。
試験対象装置へ `device_action` 内で別途送るコマンドは、その制御関数側で記録してください。

モジュールとして組み込む場合もPython標準の `logging` で出力先を指定できます。
以下を接続前に設定してください。ファイル保存なら `StreamHandler` を
`logging.FileHandler("commands.log", encoding="utf-8")` に置き換えます。

```python
import logging
import sys

command_logger = logging.getLogger("md1230b.commands")
command_logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
command_logger.addHandler(handler)
command_logger.propagate = False
# この後で with Md1230b(...) を実行。
# アプリケーション終了時に command_logger.removeHandler(handler); handler.close()。
```

## 寿命と操作の順序

1. `Md1230b.__enter__`: 接続し、Unit/Module/Portを指定して所有権を取得。
   途中のポート予約に失敗したら、既に取得したポートを解放する。
2. シナリオの入力から `EthernetFrame` を作る。これは純粋なPython処理。
3. `configure_tx`: 送信停止、有限送信ストリームを設定してWRITE。まだ送信しない。
4. `capture.__enter__`: 古い取り込みを停止、フィルター/トリガーをOFF、両ポートを順番に開始。
   状態が1または2になったことを確認してから呼出側に戻る。
5. 試験対象装置の操作、送信開始、送信停止待ち、観測時間の待機。
6. `capture.__exit__`: 全予約ポートの送信を停止し、全対象キャプチャを停止。
   状態0になるまで待つ。状態3は装置内解析中なので読出しを始めない。
   両ポートの停止後、フレームを1件ずつ取得する。
7. 呼出側で判定・結果保存。次のシナリオも同じ予約の中で実行できる。
8. `Md1230b.__exit__`: 全予約ポートの送信/キャプチャ停止、所有権解放、TCP切断。

キャプチャ開始後に `configure_tx` を呼ぶこともできますが、設定処理中にも受信データが
蓄積するため、通常は送信準備を先に済ませます。2ポートの開始/停止は同時ではありません。
本実装は、両方の開始を確認してから刺激を加える機能試験向けです。

## 返すデータと判定

- `cap.results["PortA"]` / `["PortB"]`: `CaptureResult`。PortA→PortBの受信確認はPortB、逆方向はPortA。
- `.frames`: `CapturedFrame` のタプル。`.data` は取得した生のbytes。
- `.reported_length`: 装置のLENGTH応答。取得bytes長とは別に保持する。
- `.timestamp_utc_seconds`: 装置応答のUTC日内秒をDecimalで保持。Pythonの単調時計とは比較しない。
- `.reported_count`: 装置のキャプチャバッファ件数。
- `.matching(frame)` / `.count(frame)`: タグなしEthernetのDA/SA/Type/payloadの先頭一致。
  パディング/FCSは比較しない。長さ、VLAN変換、FCS、独自フィールドの判定は呼出側で実装する。
- `.state_before_stop`: 停止指示の直前に観測した装置の状態。
- `.loss_checked`: この版では常にFalse。装置が取りこぼしていないことの確認は未実装。

同一パターンをcount回送る機能です。フレームごとの連番や複数ストリームの一括送信は未実装。
試験IDに加えて実行ごとに変わる値をpayloadに入れると、前の試験との混同を避けられます。
`example.py` はUUIDを使用します。キャプチャ開始時のバッファ更新挙動は実機で確認してください。

**「一致フレーム0件」だけで破棄試験の合格を確定しないでください。**
実送信数、非マッチフレームの疎通、試験対象装置側の受信/ヒット/破棄カウンターなどを組み合わせます。
ソフトウェア上限 `max_frames` の超過はエラーにしますが、装置内部のバッファ満杯や
上書き/取りこぼしの全検出を意味しません。短いキャプチャと少数フレームから確認してください。

## 通信・例外・回帰試験

- 設定コマンドの応答は待たず、SYST:ERR?で装置エラーを確認する。
- 問い合わせは改行まで受信する。TCP recvの1回分を1応答と決めつけない。
- 既定のコマンド間隔50msは試験用の余裕で、メーカー指定値ではない。
  `command_gap=0.1` なら100ms。これはフレーム送信レートではない。
- エラーキューが空であることは全非同期処理の完了を保証しない。
  設定の読み戻し、Tx状態、Capture状態を別途確認する。
- 対象ポート選択から操作完了までRLockで保護する。ただし試験の同時実行は非対応。
  対象装置をGUI/別プロセスから同時操作しない。
- タイムアウト/通信断後の接続は使用不可にする。遅延応答の取り違えを避けるため
  自動リトライ/再接続/別セッションからの強制解放は行わない。
- `AnalyzerError` はドライバーエラー、`DeviceError` は装置が返したエラー、
  `CleanupError` は後処理またはキャプチャ取得の失敗。
  ソケットのOSError/TimeoutErrorも呼出側に伝わる。
- 本体の例外を後処理の例外で上書きしない。後処理失敗はloggingと
  `cap.errors` / `lan.cleanup_errors` に残す。Python 3.11以上では元例外にもnoteを付ける。
- 通信が正常なシナリオ判定失敗なら、結果を記録して次のシナリオへ進められる。
  停止失敗/通信断時は同じ装置を使う後続試験を実行不能として扱う。
- 停止命令または停止確認に失敗したセッションは、後処理以外の操作を禁止する。
  `stop_tx` / `wait_tx_stopped` / `configure_tx` 内の停止も対象とし、
  後処理で停止できても、そのセッションでは後続試験を開始しない。
- キャプチャ終了時は、停止前の状態取得に失敗しても停止を試み、失敗を`cap.errors`に残す。
  全対象の停止が確認できなければ、全ポートの読出しを中止する。
  全対象が停止できた場合も、停止前の状態が取得できなかったポートの結果は返さない。
  状態取得の失敗後は、後処理以外の操作を禁止する。
- プロセス強制終了・電源断・ネットワーク断では解放を保証できない。

## この版の範囲と実機で確認する点

- MD1230Bの通常Ethernet制御。対応コマンドは搭載MUモジュール/ソフトウェア版に依存。
  PROGRAM非対応のMU120101Aは対象外。
- 一つの装置接続内で複数ポートを扱う。別筐体なら別のMd1230bインスタンスを使う。
- タグなしEthernet、固定SA/DA/Type/フレーム長、任意bytes、有限送信。
- `frame_length` はFSIZEへ渡す値。フレーム長のFCS包含、キャプチャのFCS包含/切詰め、
  Datafield1 offset=0の位置、送信内容は実機で少数フレームを取り確認する。
  入力検証ではヘッダ/FCS用に保守的に18byteを確保している。
- モジュール固有の最大フレーム長/パターン領域制限は装置エラーとして検出する。
- Txレート/IFGは指定せず、新規ストリームの既定値を使う。
- 有限送信はDISTribution STOP、FPBurst=count、BPSTream=1を使用。
  `wait_tx_stopped` は停止状態を確認するだけなので実送信数は別途カウンターで確認する。
- キャプチャフィルター/トリガーはOFFにする。元の装置設定は復元しない。
- ストリーム置換は `replace_streams=True` の場合のみ。
- フレームデータ/長さ/タイムスタンプを逐次問い合わせるため、大量キャプチャの読出しは遅い。
  PCAP出力、キャプチャハードウェアフィルター設定、試験対象装置制御そのものは含めていない。

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| md1230b/transport.py | SCPI通信・送信コマンド履歴・エラー解析・接続の利用可否 |
| md1230b/models.py | ポート/送信フレーム定義とキャプチャ結果 |
| md1230b/driver.py | 予約、送信、キャプチャ、後処理 |
| example.py | 試験対象装置の操作を差し込める双方向試験・ログ出力先の指定 |
| tests/test_driver.py | 模擬装置による寿命管理/失敗時の検証 |
| tests/test_transport.py | 分割受信/タイムアウト後の再利用禁止の検証 |
| tests/test_example.py | 標準出力/ファイル追記・例外時のコマンド履歴の検証 |

```bash
python -m unittest discover -s tests -v
```

## 参照仕様

Anritsu MD1230B Remote Control Operation Manual, M-W1929AE-24.0, 24th Edition:
https://www.scribd.com/document/964529483/RemoteCommand-1230

- Port ownership: pp.6-149, 6-151
- Tx stream WRITE / ADD / ID: pp.6-488〜6-490
- Finite stream control: pp.6-491〜6-492
- Programmable data: pp.6-509〜6-511
- Capture state: p.6-643 (0停止、1取り込み、2トリガー後取り込み、3解析)
- Capture frame count/data/length/timestamp: pp.6-646〜6-647

模擬テストはPython側の制御フローを確認するもので、実機互換性の確認ではありません。
