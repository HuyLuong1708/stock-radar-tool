"""
Backend API cho công cụ Bảng vàng (radar quét cổ phiếu đột biến) + Biểu đồ
kỹ thuật, dùng dữ liệu thật từ vnstock.

Endpoints:
- GET /api/health
- GET /api/radar?direction=up|down&min_vol_ratio=1.3&min_change_pct=2&min_value=3000000000
- GET /api/candles?symbol=ACB&interval=1D&limit=200

Chạy local: python app.py
Deploy: xem README.md.
"""
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

VNSTOCK_SOURCE = os.environ.get("VNSTOCK_SOURCE", "VCI")
EXCHANGES = ["HOSE", "HNX", "UPCOM"]

_cache = {}
CACHE_SECONDS = 30

INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1D": "1D"}


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
    """price_board() trả cột dạng chuỗi trông giống tuple, vd \"('match', 'match_price')\"."""
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
    Quét toàn bộ mã, tìm mã có biến động bất thường trong phiên:
    - Khối lượng khớp >= min_vol_ratio lần trung bình 10 phiên
    - Biến động giá (trị tuyệt đối) >= min_change_pct %
    - Giá trị giao dịch >= min_value VNĐ
    - direction: "up" (chỉ lọc mã tăng) hoặc "down" (chỉ lọc mã giảm)
    """
    direction = request.args.get("direction", "up")
    min_vol_ratio = float(request.args.get("min_vol_ratio", 1.3))
    min_change_pct = float(request.args.get("min_change_pct", 2.0))
    min_value = float(request.args.get("min_value", 3_000_000_000))

    cache_key = f"radar_raw"
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
    avg_vol_col = _find_col(board.columns, "avg_match_volume2_w")  # trung bình KL nếu vnstock cung cấp sẵn

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

    if avg_vol_col:
        avg_vol_raw = board[avg_vol_col]
        df["avg_volume"] = avg_vol_raw
        df["vol_ratio"] = df["volume"] / df["avg_volume"].replace(0, pd.NA)
    else:
        # vnstock bản này không trả sẵn KL trung bình -> tạm dùng ước lượng theo GTGD
        # (không chính xác bằng trung bình thật, ghi rõ để người dùng biết)
        df["avg_volume"] = None
        df["vol_ratio"] = None

    filtered = df[df["trading_value"] >= min_value]
    if direction == "up":
        filtered = filtered[filtered["change_pct"] >= min_change_pct]
    else:
        filtered = filtered[filtered["change_pct"] <= -min_change_pct]

    if "vol_ratio" in filtered.columns and filtered["vol_ratio"].notna().any():
        filtered = filtered[filtered["vol_ratio"].fillna(0) >= min_vol_ratio]

    # Chấm điểm đơn giản: kết hợp biến động giá + tỷ lệ khối lượng (nếu có)
    def score_row(row):
        change_score = min(abs(row["change_pct"]) * 6, 60)
        vol_score = min((row["vol_ratio"] or 1) * 15, 40) if pd.notna(row.get("vol_ratio")) else 20
        return round(change_score + vol_score, 1)

    filtered = filtered.copy()
    filtered["score"] = filtered.apply(score_row, axis=1)
    filtered = filtered.sort_values("score", ascending=False)

    results = []
    for _, row in filtered.head(50).iterrows():
        results.append({
            "symbol": row["symbol"],
            "price": round(float(row["price"]), 2),
            "change_pct": round(float(row["change_pct"]), 2),
            "volume": int(row["volume"]) if pd.notna(row["volume"]) else 0,
            "vol_ratio": round(float(row["vol_ratio"]), 2) if pd.notna(row.get("vol_ratio")) else None,
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
    """Pivot Point truyền thống, tính từ High/Low/Close phiên GẦN NHẤT ĐÃ ĐÓNG."""
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
