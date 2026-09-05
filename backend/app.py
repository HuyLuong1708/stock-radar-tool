"""
Backend API cho công cụ Bảng vàng (radar quét cổ phiếu đột biến) + Biểu đồ
kỹ thuật + Trend Template (Mark Minervini), dùng dữ liệu thật từ vnstock.

Endpoints:
- GET /api/health
- GET /api/radar?direction=up|down&min_vol_ratio=1.3&min_change_pct=2&min_value=3000000000
- GET /api/candles?symbol=ACB&interval=1D&limit=200
- GET /api/trend_template?symbol=ACB

Chạy local: python app.py
"""
import os
import time
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

VNSTOCK_SOURCE = os.environ.get("VNSTOCK_SOURCE", "VCI")
EXCHANGES = ["HOSE", "HNX", "UPCOM"]

_cache = {}
CACHE_SECONDS = 30


def register_vnstock_key():
    api_key = os.environ.get("VNSTOCK_API_KEY", "")
    if not api_key:
        return
    try:
        from vnstock import register_user
        register_user(api_key=api_key)
    except Exception as e:
        print(f"[WARN] Không đăng ký được VNSTOCK_API_KEY: {e}")


register_vnstock_key()


def _find_col(columns, target_word):
    target_quoted = f"'{target_word}'"
    candidates = [c for c in columns if target_quoted in str(c)]
    return candidates[0] if candidates else None


def get_all_symbols() -> pd.DataFrame:
    from vnstock import Listing
    listing = Listing(source=VNSTOCK_SOURCE)
    df = listing.symbols_by_exchange()
    df.columns = [c.lower() for c in df.columns]
    df = df[df["exchange"].isin(EXCHANGES)]
    return df.reset_index(drop=True)


def get_price_board(symbols: list) -> pd.DataFrame:
    from vnstock import Trading
    trading = Trading(source=VNSTOCK_SOURCE)
    frames = []
    batch_size = 100
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        try:
            df = trading.price_board(batch)
            df.columns = [str(c).lower() for c in df.columns]
            frames.append(df)
        except Exception as e:
            print(f"[WARN] Lỗi price board batch {i}: {e}")
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_daily_history(symbol: str, days: int = 60) -> pd.DataFrame:
    from vnstock import Quote
    end = pd.Timestamp.today()
    start = end - pd.Timedelta(days=days * 2)
    try:
        quote = Quote(symbol=symbol, source=VNSTOCK_SOURCE)
        df = quote.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), interval="1D")
        df.columns = [str(c).lower() for c in df.columns]
        time_col = "time" if "time" in df.columns else df.columns[0]
        df = df.sort_values(time_col).reset_index(drop=True)
        return df.tail(days)
    except Exception as e:
        print(f"[WARN] Không lấy được lịch sử cho {symbol}: {e}")
        return pd.DataFrame()


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/radar")
def radar():
    """
    Quét toàn bộ mã, tìm mã có biến động bất thường trong phiên.
    Sau khi lọc thô (theo % biến động + GTGD, dùng price_board - nhanh),
    với các mã LỌT QUA vòng lọc đầu, gọi thêm dữ liệu lịch sử 10 phiên để
    tính khối lượng trung bình THẬT, rồi lọc lại theo KL/TB chính xác.
    Cách này tránh phải gọi lịch sử cho toàn bộ ~1100 mã (quá chậm, dễ vượt
    giới hạn API) mà vẫn cho ra số liệu KL/TB đúng cho kết quả cuối cùng.
    """
    direction = request.args.get("direction", "up")
    min_vol_ratio = float(request.args.get("min_vol_ratio", 1.3))
    min_change_pct = float(request.args.get("min_change_pct", 2.0))
    min_value = float(request.args.get("min_value", 3_000_000_000))

    cache_key = "radar_raw"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"] < CACHE_SECONDS):
        board = cached["data"]
    else:
        symbols_df = get_all_symbols()
        all_symbols = symbols_df["symbol"].tolist()
        board = get_price_board(all_symbols)
        if board.empty:
            return jsonify({"error": "Không lấy được price board."}), 502
        _cache[cache_key] = {"ts": now, "data": board}

    symbol_col = _find_col(board.columns, "symbol")
    price_col = _find_col(board.columns, "match_price")
    vol_col = _find_col(board.columns, "accumulated_volume")
    ref_col = _find_col(board.columns, "ref_price")

    if not symbol_col or not price_col or not vol_col or not ref_col:
        return jsonify({
            "error": "Thiếu cột dữ liệu cần thiết.",
            "columns_available": [str(c) for c in board.columns],
        }), 500

    df = board[[symbol_col, price_col, vol_col, ref_col]].copy()
    df.columns = ["symbol", "price", "volume", "ref_price"]
    df = df.dropna(subset=["symbol", "price", "ref_price"])
    df = df[(df["price"] > 0) & (df["ref_price"] > 0)]

    df["change_pct"] = (df["price"] - df["ref_price"]) / df["ref_price"] * 100
    df["trading_value"] = df["price"] * df["volume"]

    filtered = df[df["trading_value"] >= min_value]
    if direction == "up":
        filtered = filtered[filtered["change_pct"] >= min_change_pct]
    else:
        filtered = filtered[filtered["change_pct"] <= -min_change_pct]

    filtered = filtered.sort_values("trading_value", ascending=False).head(60)

    vol_ratio_map = {}
    for symbol in filtered["symbol"]:
        hist = get_daily_history(symbol, days=15)
        if hist.empty or len(hist) < 6:
            continue
        vol_col_hist = "volume" if "volume" in hist.columns else "v"
        avg_vol_10 = hist[vol_col_hist].iloc[-11:-1].mean() if len(hist) >= 11 else hist[vol_col_hist].iloc[:-1].mean()
        today_vol = filtered.loc[filtered["symbol"] == symbol, "volume"].values[0]
        if avg_vol_10 and avg_vol_10 > 0:
            vol_ratio_map[symbol] = today_vol / avg_vol_10

    filtered = filtered.copy()
    filtered["vol_ratio"] = filtered["symbol"].map(vol_ratio_map)

    filtered = filtered[filtered["vol_ratio"].fillna(0) >= min_vol_ratio]

    def score_row(row):
        change_score = min(abs(row["change_pct"]) * 6, 60)
        vol_score = min((row["vol_ratio"] or 1) * 15, 40)
        return round(change_score + vol_score, 1)

    filtered["score"] = filtered.apply(score_row, axis=1)
    filtered = filtered.sort_values("score", ascending=False)

    results = []
    for _, row in filtered.head(30).iterrows():
        results.append({
            "symbol": row["symbol"],
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row["change_pct"]), 2),
            "volume": int(row["volume"]) if pd.notna(row["volume"]) else 0,
            "vol_ratio": round(float(row["vol_ratio"]), 2) if pd.notna(row["vol_ratio"]) else None,
            "trading_value": int(row["trading_value"]),
            "score": row["score"],
        })

    return jsonify({
        "direction": direction,
        "count": len(results),
        "total_scanned": len(df),
        "fetched_at": datetime.now().isoformat(),
        "results": results,
    })


def compute_pivot_points(df: pd.DataFrame) -> dict:
    if len(df) < 2:
        return {}
    prev = df.iloc[-2]
    high, low, close = prev["high"], prev["low"], prev["close"]
    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2 * (pivot - low)
    s3 = low - 2 * (high - pivot)
    return {
        "pivot": round(pivot, 2), "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
        "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
    }


def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["close"]
    ma20 = close.rolling(20).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi14 = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line

    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    def clean(series):
        return [None if pd.isna(v) else round(float(v), 2) for v in series]

    return {
        "ma20": clean(ma20), "ema20": clean(ema20), "rsi14": clean(rsi14),
        "macd": clean(macd_line), "macd_signal": clean(signal_line), "macd_hist": clean(macd_hist),
        "bb_upper": clean(bb_upper), "bb_mid": clean(bb_mid), "bb_lower": clean(bb_lower),
        "pivot_points": compute_pivot_points(df),
    }


@app.route("/api/candles")
def candles():
    symbol = request.args.get("symbol", "").upper().strip()
    limit = min(int(request.args.get("limit", 200)), 500)
    if not symbol:
        return jsonify({"error": "Thiếu tham số symbol."}), 400

    cache_key = f"candles:{symbol}:{limit}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"] < CACHE_SECONDS):
        return jsonify(cached["data"])

    df = get_daily_history(symbol, days=limit)
    if df.empty:
        return jsonify({"error": f"Không lấy được dữ liệu cho {symbol}."}), 502

    open_col = "open" if "open" in df.columns else "o"
    high_col = "high" if "high" in df.columns else "h"
    low_col = "low" if "low" in df.columns else "l"
    close_col = "close" if "close" in df.columns else "c"
    vol_col = "volume" if "volume" in df.columns else "v"

    df_std = df.rename(columns={open_col: "open", high_col: "high", low_col: "low", close_col: "close", vol_col: "volume"})

    ohlcv = []
    for _, row in df_std.iterrows():
        ts = int(pd.Timestamp(row["time"]).timestamp())
        ohlcv.append({
            "time": ts, "open": round(float(row["open"]), 2), "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2), "close": round(float(row["close"]), 2),
            "volume": int(row["volume"]),
        })

    indicators = compute_indicators(df_std)
    result = {"symbol": symbol, "candles": ohlcv, "indicators": indicators, "fetched_at": datetime.now().isoformat()}
    _cache[cache_key] = {"ts": now, "data": result}
    return jsonify(result)


def compute_trend_template(df: pd.DataFrame) -> dict:
    """
    Trend Template (Mark Minervini) - 8 tiêu chí đánh giá cổ phiếu đang
    trong giai đoạn tăng trưởng mạnh (Stage 2). Cần tối thiểu ~260 phiên
    (khoảng 1 năm) để tính đủ MA200 + đỉnh/đáy 52 tuần.

    LƯU Ý: đây là bộ tiêu chí phổ biến trong sách "Trade Like a Stock Market
    Wizard" của Minervini, được mã hoá lại theo hiểu biết chung, không phải
    công cụ chính thức của tác giả. Tiêu chí RS Rating (so với thị trường
    chung) cần dữ liệu VN-Index để tính chính xác — hiện CHƯA triển khai,
    đánh dấu "n/a" để không đưa ra kết luận sai.
    """
    if len(df) < 210:
        return {"error": "Chưa đủ dữ liệu lịch sử (cần tối thiểu ~210 phiên) để đánh giá Trend Template."}

    close = df["close"]
    ma50 = close.rolling(50).mean()
    ma150 = close.rolling(150).mean()
    ma200 = close.rolling(200).mean()

    last_close = close.iloc[-1]
    last_ma50 = ma50.iloc[-1]
    last_ma150 = ma150.iloc[-1]
    last_ma200 = ma200.iloc[-1]
    ma200_1m_ago = ma200.iloc[-22] if len(ma200) > 22 else None

    low_52w = df["low"].tail(252).min() if len(df) >= 252 else df["low"].min()
    high_52w = df["high"].tail(252).max() if len(df) >= 252 else df["high"].max()

    criteria = []

    c1 = bool(last_close > last_ma150 and last_close > last_ma200)
    criteria.append({"id": 1, "label": "Giá > MA150 và giá > MA200", "pass": c1,
                      "detail": f"Giá {last_close:.2f} / MA150 {last_ma150:.2f} / MA200 {last_ma200:.2f}"})

    c2 = bool(last_ma150 > last_ma200)
    criteria.append({"id": 2, "label": "MA150 > MA200", "pass": c2,
                      "detail": f"MA150 {last_ma150:.2f} vs MA200 {last_ma200:.2f}"})

    c3 = bool(ma200_1m_ago is not None and last_ma200 > ma200_1m_ago)
    criteria.append({"id": 3, "label": "MA200 dốc lên (≥1 tháng)", "pass": c3,
                      "detail": f"MA200 hiện {last_ma200:.2f} vs 1 tháng trước {ma200_1m_ago:.2f}" if ma200_1m_ago else "Chưa đủ dữ liệu"})

    c4 = bool(last_ma50 > last_ma150 > last_ma200)
    criteria.append({"id": 4, "label": "MA50 > MA150 > MA200", "pass": c4,
                      "detail": f"MA50 {last_ma50:.2f} / MA150 {last_ma150:.2f} / MA200 {last_ma200:.2f}"})

    c5 = bool(last_close > last_ma50)
    criteria.append({"id": 5, "label": "Giá > MA50", "pass": c5,
                      "detail": f"Giá {last_close:.2f} vs MA50 {last_ma50:.2f}"})

    pct_above_low = (last_close - low_52w) / low_52w * 100 if low_52w > 0 else 0
    c6 = bool(pct_above_low >= 30)
    criteria.append({"id": 6, "label": "Giá cao hơn đáy 52 tuần ≥ 30%", "pass": c6,
                      "detail": f"Cao hơn đáy 52 tuần {pct_above_low:.1f}% (đáy: {low_52w:.2f})"})

    pct_below_high = (high_52w - last_close) / high_52w * 100 if high_52w > 0 else 0
    c7 = bool(pct_below_high <= 25)
    criteria.append({"id": 7, "label": "Giá cách đỉnh 52 tuần ≤ 25%", "pass": c7,
                      "detail": f"Cách đỉnh 52 tuần {pct_below_high:.1f}% (đỉnh: {high_52w:.2f})"})

    criteria.append({"id": 8, "label": "RS Rating so với thị trường (chưa triển khai)", "pass": None,
                      "detail": "Cần dữ liệu VN-Index để tính Relative Strength - đánh dấu n/a"})

    passed = sum(1 for c in criteria if c["pass"] is True)
    total_evaluable = sum(1 for c in criteria if c["pass"] is not None)

    return {
        "passed": passed,
        "total_evaluable": total_evaluable,
        "criteria": criteria,
        "summary": f"{passed}/{total_evaluable} tiêu chí đạt (không tính RS Rating - chưa triển khai)",
    }


@app.route("/api/trend_template")
def trend_template():
    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "Thiếu tham số symbol."}), 400

    df = get_daily_history(symbol, days=260)
    if df.empty:
        return jsonify({"error": f"Không lấy được dữ liệu cho {symbol}."}), 502

    close_col = "close" if "close" in df.columns else "c"
    high_col = "high" if "high" in df.columns else "h"
    low_col = "low" if "low" in df.columns else "l"
    df_std = df.rename(columns={close_col: "close", high_col: "high", low_col: "low"})

    result = compute_trend_template(df_std)
    result["symbol"] = symbol
    result["fetched_at"] = datetime.now().isoformat()
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
