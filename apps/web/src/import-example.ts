/* Original artificial numbers; reuse permitted in public/examples/README.txt. */
export const SYNTHETIC_CSV = "schema_version,venue,symbol,interval_start,interval_end,adjustment,open,high,low,close,volume,source,source_sequence,revision,available_at\n1,XNAS,AAPL,2026-01-02T14:30:00.000000Z,2026-01-02T14:31:00.000000Z,raw,100,101,99,100,10,quine.synthetic.tutorial,0,0,2026-01-02T14:31:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:31:00.000000Z,2026-01-02T14:32:00.000000Z,raw,101,102,100,101,10,quine.synthetic.tutorial,1,0,2026-01-02T14:32:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:32:00.000000Z,2026-01-02T14:33:00.000000Z,raw,102,103,101,102,10,quine.synthetic.tutorial,2,0,2026-01-02T14:33:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:33:00.000000Z,2026-01-02T14:34:00.000000Z,raw,103,104,102,103,10,quine.synthetic.tutorial,3,0,2026-01-02T14:34:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:34:00.000000Z,2026-01-02T14:35:00.000000Z,raw,104,105,103,104,10,quine.synthetic.tutorial,4,0,2026-01-02T14:35:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:35:00.000000Z,2026-01-02T14:36:00.000000Z,raw,105,106,104,105,10,quine.synthetic.tutorial,5,0,2026-01-02T14:36:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:36:00.000000Z,2026-01-02T14:37:00.000000Z,raw,106,107,105,106,10,quine.synthetic.tutorial,6,0,2026-01-02T14:37:00.000000Z\n1,XNAS,AAPL,2026-01-02T14:37:00.000000Z,2026-01-02T14:38:00.000000Z,raw,107,108,106,107,10,quine.synthetic.tutorial,7,0,2026-01-02T14:38:00.000000Z\n"
export const SYNTHETIC_SOURCE = "QUINE 原创合成教学示例（2026-10-18）；非真实市场数据"
export const SYNTHETIC_LICENSE = "作者允许无条件复制、修改和使用；见 examples/README.txt"
export const SYNTHETIC_NOTICE = `QUINE 合成教学示例 · 2026-10-18
8 行 OHLCV 数字由 QUINE 原创构造，无第三方行情来源，不是真实市场观察。
作者允许任何人无条件复制、修改和使用这些示例数据。
UTC 时间和 available_at 均为演示声明，PIT（当时可获得性）未验证。
标准示例使用固定 15 列；自定义列名示例可用“填入合成映射示例”查看并复制其声明。
XNAS:AAPL 仅是兼容标记，不表示交易所行情或投资效果证据。`
