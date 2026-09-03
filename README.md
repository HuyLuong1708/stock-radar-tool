# Bảng vàng & Biểu đồ kỹ thuật (Radar quét cổ phiếu đột biến)

Công cụ web gồm 2 phần:
1. **Bảng vàng** — quét toàn bộ mã HOSE/HNX/UPCOM, lọc mã có biến động giá +
   khối lượng bất thường trong phiên, chấm điểm và xếp hạng.
2. **Biểu đồ kỹ thuật** — biểu đồ nến + MA/EMA/Bollinger/RSI/Volume + Pivot
   Points (Classic), dùng dữ liệu thật từ vnstock.

⚠️ Đây là công cụ hỗ trợ tham khảo, không phải khuyến nghị đầu tư. Điểm số
trong Bảng vàng là công thức tự xây dựng đơn giản (kết hợp % biến động +
tỷ lệ khối lượng), không phải thuật toán độc quyền của bất kỳ nền tảng nào.

## Cấu trúc

```
stock_radar/
├── backend/
│   ├── app.py              # Flask API (radar + candles + pivot points)
│   └── requirements.txt
└── frontend/
    └── index.html          # Giao diện web (2 tab: Bảng vàng / Biểu đồ)
```

## Deploy backend lên Render (miễn phí)

Quy trình giống hệt bot VN30F1M bạn đã làm trước đây:

1. Đẩy code lên GitHub repo (riêng, hoặc chung với bot Telegram nếu muốn — chỉ cần khác thư mục)
2. Vào [render.com](https://render.com) → **New → Web Service** → kết nối repo
3. **Root Directory:** `backend` (nếu để chung repo với bot Telegram)
4. **Build Command:** `pip install -r requirements.txt`
5. **Start Command:** `gunicorn app:app`
6. Thêm biến môi trường (Environment Variables) nếu có API key vnstock:
   - `VNSTOCK_API_KEY` = key của bạn (khuyến nghị, tránh giới hạn 20 request/phút)
7. Deploy — Render sẽ cấp cho bạn 1 URL dạng `https://ten-app.onrender.com`

## Deploy frontend

Cách đơn giản nhất: **GitHub Pages** (miễn phí, tĩnh, không cần server riêng)

1. Trong file `frontend/index.html`, thêm dòng sau vào đầu thẻ `<script>` cuối cùng
   (trước dòng `const API_BASE = ...`):
   ```html
   <script>window.RADAR_API_BASE = 'https://ten-app.onrender.com';</script>
   ```
   Thay đúng URL backend bạn vừa lấy được từ Render ở bước trên.
2. Vào repo GitHub → **Settings → Pages** → chọn nhánh `main`, thư mục `/frontend` (hoặc `/`)
3. GitHub cấp cho bạn 1 URL dạng `https://<username>.github.io/<repo>/`

## Giới hạn cần biết

- **Render free tier:** server "ngủ" sau 15 phút không ai truy cập, lần mở
  tiếp theo mất ~30-50 giây để "thức dậy". Chấp nhận được cho dùng cá nhân.
- **Radar quét toàn bộ ~1100+ mã mỗi lần** — có thể mất vài giây tới vài chục
  giây tuỳ tốc độ API vnstock. Kết quả được cache 30 giây để tránh gọi API
  quá thường xuyên.
- **Cột "avg_volume" (khối lượng trung bình)** phụ thuộc vào việc vnstock có
  trả sẵn trường này trong `price_board()` hay không — nếu không có, tỷ lệ
  KL/TB sẽ hiển thị "—" và bộ lọc theo khối lượng sẽ không áp dụng được
  chính xác. Xem mục "Kiểm tra dữ liệu" bên dưới nếu gặp trường hợp này.
- **Pivot Points dùng công thức Classic**, khác với công thức "Pivot Pro"
  của TradingView (có thể là biến thể Camarilla/Woodie) — số sẽ không khớp
  1:1 nếu so sánh với TradingView.

## Kiểm tra dữ liệu trước khi dùng thật

Thư viện vnstock hay đổi cấu trúc dữ liệu giữa các phiên bản. Trước khi tin
tưởng kết quả, chạy thử trên máy có internet:

```bash
cd backend
pip install -r requirements.txt
python -c "
from app import get_all_symbols, get_price_board
syms = get_all_symbols()
board = get_price_board(syms['symbol'].head(20).tolist())
print(list(board.columns))
print(board.head())
"
```

Nếu không thấy cột chứa `avg_match_volume2_w` hoặc tên tương tự, cần điều
chỉnh lại tên cột `avg_vol_col` trong `app.py` (hàm `radar()`) cho khớp với
tên cột thực tế trả về.
