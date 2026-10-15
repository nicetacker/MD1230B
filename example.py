"""実行すると指定した試験用ポートのTx Stream設定を置換して送信します。"""
import argparse
import logging
import sys
import uuid

from md1230b import EthernetFrame, Md1230b, PortAddress


def bidirectional_test(lan, device_action=lambda: None):
    """試験対象装置の制御関数をdevice_actionに渡せる。操作の内容は呼出側が所有。"""
    token = uuid.uuid4().bytes
    a_to_b = EthernetFrame(sa="02:00:00:00:00:01", da="02:00:00:00:00:02",
                           ethertype=0x88B5, payload=b"A_TO_B" + token, frame_length=128)
    b_to_a = EthernetFrame(sa=a_to_b.da, da=a_to_b.sa, ethertype=a_to_b.ethertype,
                           payload=b"B_TO_A" + token, frame_length=128)
    # 時間のかかる装置設定はキャプチャ前に済ませる。
    # この例では対象ポートの既存Tx Streamを削除する。
    lan["PortA"].configure_tx(a_to_b, count=10, replace_streams=True)
    lan["PortB"].configure_tx(b_to_a, count=10, replace_streams=True)

    capture = lan.capture("PortA", "PortB", max_frames=1000)
    with capture:
        # ここでは両ポートのキャプチャが動作中。
        device_action()
        capture.check_running()
        lan["PortA"].start_tx()
        lan["PortA"].wait_tx_stopped()
        lan["PortB"].start_tx()
        lan["PortB"].wait_tx_stopped()
        capture.observe(0.5)  # シナリオで定めた追加の観測時間。

    # 両方のキャプチャ停止・装置内解析・データ読出しが完了した後。
    received_a_to_b = capture.results["PortB"].count(a_to_b)
    received_b_to_a = capture.results["PortA"].count(b_to_a)
    if received_a_to_b != 10 or received_b_to_a != 10:
        raise AssertionError(f"疎通失敗: PortA→PortB={received_a_to_b}/10, PortB→PortA={received_b_to_a}/10")
    return capture.results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--unit", type=int, required=True)
    parser.add_argument("--module", type=int, required=True)
    parser.add_argument("--port-a", type=int, required=True)
    parser.add_argument("--port-b", type=int, required=True)
    parser.add_argument("--tcp-port", type=int, default=5000)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--command-log", metavar="PATH|-",
                        help="送信コマンド全文をUTF-8ファイルに追記。- なら標準出力")
    args = parser.parse_args(argv)
    log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        stream=sys.stdout, format=log_format)
    ports = {
        "PortA": PortAddress(args.unit, args.module, args.port_a),
        "PortB": PortAddress(args.unit, args.module, args.port_b),
    }
    command_logger = logging.getLogger("md1230b.commands")
    previous_level, previous_propagate = command_logger.level, command_logger.propagate
    handler = None
    try:
        command_logger.setLevel(logging.INFO if args.command_log or args.verbose else logging.WARNING)
        if args.command_log:
            handler = (logging.StreamHandler(sys.stdout) if args.command_log == "-" else
                       logging.FileHandler(args.command_log, mode="a", encoding="utf-8"))
            handler.setFormatter(logging.Formatter(log_format))
            command_logger.addHandler(handler)
            command_logger.propagate = False
        with Md1230b(args.host, ports, tcp_port=args.tcp_port) as lan:
            result = bidirectional_test(lan)
            print({name: data.reported_count for name, data in result.items()})
    finally:
        if handler is not None:
            command_logger.removeHandler(handler)
            handler.close()
        command_logger.setLevel(previous_level)
        command_logger.propagate = previous_propagate


if __name__ == "__main__":
    main()
