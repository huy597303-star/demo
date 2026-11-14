# SocialStock Bot V5

Telegram bot quản lý kho hàng số, đơn hàng, số dư và báo cáo. V5 giữ lại hai bảng `users` và `accounts` của bản cũ, tự thêm cột/bảng cần thiết khi khởi động.

## 1. Bảo mật trước khi chạy
- Token Telegram và SePay từng xuất hiện trong source cũ. Hãy thu hồi/cấp lại trước khi triển khai.
- Không đưa `.env` hoặc token vào GitHub.
- Chỉ quản lý hàng hóa bạn có quyền kinh doanh và dữ liệu được phép lưu trữ.

## 2. Chạy trên Windows
```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```
Mở `.env`, điền `BOT_TOKEN`, `ADMIN_IDS`, `DB_PATH`, `SEPAY_WEBHOOK_SECRET`, sau đó:
```bat
python bot.py
```
Nếu máy chưa có Python launcher `py`, dùng `python` thay thế.

## 3. Deploy Render
1. Tạo GitHub repository mới hoặc cập nhật repository hiện tại.
2. Upload `bot.py`, `requirements.txt`, `render.yaml`, `.gitignore`.
3. Render → New → Blueprint → chọn repository.
4. Render tạo Web Service và Persistent Disk theo `render.yaml`.
5. Trong Environment, nhập `BOT_TOKEN` mới và `SEPAY_WEBHOOK_SECRET`.
6. Deploy. Kiểm tra URL gốc trả JSON `SocialStock V5 / running`.
7. Cấu hình webhook SePay trỏ tới `https://TEN-MIEN.onrender.com/sepay-webhook`.

**Lưu ý:** Persistent Disk cần gói Render hỗ trợ disk trả phí. Không lưu SQLite trên filesystem tạm của instance, vì dữ liệu có thể mất khi deploy/restart.

## 4. Lệnh Telegram
- `/start`, `/help`
- `/admin`
- `/import <tên_sản_phẩm>` rồi gửi file TXT
- `/themhang Tên sản phẩm | Thông tin hàng`
- `/setgia <tên_sản_phẩm> <giá_bán> <giá_vốn>`
- `/loi <ID_hàng>`
- `/khoiphuc <ID_hàng>`
- `/cong <UID> <số_tiền> [lý_do]`
- `/tru <UID> <số_tiền> [lý_do]`
- `/hoantien <mã_đơn>`
- `/backup`

## 5. Định dạng TXT
Có thể nhập file TXT theo từng dòng:
```text
TikTok|mã hàng mẫu 001
Instagram|mã hàng mẫu 002
```
Lệnh `/import TikTok` cho phép dùng dòng chỉ chứa thông tin hàng; các dòng có `Tên|Thông tin` sẽ dùng tên ở từng dòng. Dòng trùng chính xác cùng tên và thông tin được bỏ qua.

## 6. Giữ dữ liệu cũ
Trước khi cập nhật, tải backup database hiện tại. Migration dùng `CREATE TABLE IF NOT EXISTS` và `ALTER TABLE ADD COLUMN`, không xóa bảng hoặc hàng cũ. Nếu database cũ đang ở đường dẫn khác, phải sao chép `bot_store.db` cũ vào persistent disk trước khi chạy V5 và đặt `DB_PATH` đúng.

## 7. Quan trọng về SePay
Webhook có kiểm tra mã giao dịch duy nhất để tránh cộng tiền trùng. Cần xác nhận cấu hình payload thực tế của SePay có trường `id`, `referenceCode` hoặc `transactionId`; nếu tài khoản SePay dùng tên trường khác, cần điều chỉnh theo tài liệu webhook của tài khoản. Nếu bật `SEPAY_WEBHOOK_SECRET`, cấu hình dịch vụ gửi header `Authorization: Bearer <secret>` tương ứng.
