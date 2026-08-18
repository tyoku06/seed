# home-network-diagnosis

自宅Wi-Fi/回線が遅い原因を、推測ではなく実測で切り分けるためのツール。macOS前提(Wi-Fi統計は `system_profiler`、バッファブロート測定は Apple 標準の `networkQuality` を使うため)。サードパーティのPythonパッケージは不要。

## 使い方

```bash
# 現状を測定
python3 diagnose.py --label before

# ルーターの位置やチャンネルを変える、IPoEに切り替える、などの対策を実施

# 対策後にもう一度測定
python3 diagnose.py --label after

# 1回分の結果を診断
python3 analyze.py results/xxxxx_before.json

# 前後比較
python3 analyze.py results/xxxxx_before.json results/xxxxx_after.json
```

`diagnose.py` は毎回 `results/<timestamp>_<label>.json` に測定結果一式を保存する。ラベルを変えて複数回実行すれば、対策の前後比較ができる。

## 測定項目 (`diagnose.py`)

1. **Wi-Fi**: 信号強度・ノイズ・SNR・リンク速度・規格(PHYモード)・チャンネル・帯域幅 (`system_profiler SPAirPortDataType`)
2. **IPv6**: グローバルアドレスの有無・デフォルト経路の有無 (`ifconfig` / `netstat -rn -f inet6`)
3. **遅延**: デフォルトゲートウェイ・1.1.1.1・8.8.8.8 への平均/最大/ロス率 (`ping`)
4. **経路**: 1.1.1.1 への traceroute 最初の4ホップ、IPと応答時間 (`traceroute -m 4`)
5. **DNS**: ルーター・1.1.1.1・8.8.8.8への応答時間比較(生UDPで自前実装、`dig`不要)
6. **スループット**: 大きいファイルの単一接続ダウンロードと6並列ダウンロード(Range分割)
7. **バッファブロート**: `networkQuality -s`(対応OSならJSON、非対応なら `-v` のテキスト出力をパース)のRPMとアイドル遅延

## 判定ロジック (`analyze.py`)

出力の各行には次のタグを付ける。

- `[実測]` — JSONの数値をそのまま読んだ、ツールで裏付けられる事実
- `[推測]` — その数値から導いた推論。外れている可能性がある
- `[未検証]` — このツールでは判定できない項目。手動/ISPへの確認が必要

判断順序は依頼どおり: Wi-Fi電波 → IPv6接続方式(PPPoE/IPoE疑い) → バッファブロート(RPM) → traceroute 2ホップ目。

対策は効果が大きい順に並べ、効果が無いと判断できるものは `効果なし` と明言する(例: Wi-Fiの信号品質が既に良好な場合、置き場所やチャンネル変更は効果なしと出す)。

## 既知の限界

- Wi-Fi統計とバッファブロート測定は macOS 専用。Linux/Windowsでは該当セクションが `unavailable` になるが、他の測定(ping/traceroute/DNS/スループット)は動く。
- `traceroute` の2ホップ目が「ISPの想定機器か」の判定はこのツールではできない(ISPのネットワーク構成を知らないため)。IPと応答時間のみ記録し、`[未検証]`として扱う。
- IPv6が無いことは PPPoE を **示唆する** だけで確定させるものではない。実際の接続方式はルーターのWAN設定画面かISPへの確認が必要。
- スループット計測用URL(既定: Cloudflare speed test)への到達性がない環境ではこのセクションはエラーになる。`--big-file-url` で差し替え可能。
