# ScreenCam Lite

Ứng dụng quay màn hình, webcam và âm thanh song song siêu nhẹ cho Windows (Python + CustomTkinter + FFmpeg), hỗ trợ mã hóa phần cứng Intel Quick Sync (`h264_qsv`) / MediaFoundation (`h264_mf`) / `libx264`.

---

## ⚡ Tải bản chạy ngay (Không cần cài đặt Python)

Nếu bạn chỉ muốn sử dụng app trên bất kỳ máy Windows nào mà **không cần cài đặt Python hay môi trường lập trình**:

👉 Tải file zip dựng sẵn tại: **[GitHub Releases - ScreenCam Lite v1.0.0](https://github.com/chuongnd89-jpg/screen-cam-lite/releases)**
1. Tải file `ScreenCamLite-Win64.zip`.
2. Giải nén vào một thư mục bất kỳ.
3. Click đúp vào `ScreenCamLite.exe` để sử dụng ngay!

---

## 🛠️ Hướng dẫn tạo file chạy `.exe` khi kéo mã nguồn từ Git

Do file binary `ffmpeg.exe` dung lượng lớn (>80MB) nên không lưu trực tiếp trên Git repo. Khi kéo source code về một máy tính mới, bạn có 2 cách để có file chạy:

### Cách 1: Tự động 1-Click (Khuyên dùng)
Trong thư mục source code đã có sẵn file `build.bat`:
1. Mở thư mục dự án vừa clone về.
2. Click đúp vào file **`build.bat`**.
3. Script sẽ tự động:
   - Cài đặt các thư viện Python từ `requirements.txt`.
   - Kiểm tra `bin/ffmpeg.exe`, nếu thiếu sẽ tự động tìm từ hệ thống hoặc tải về cho bạn.
   - Chạy PyInstaller đóng gói toàn bộ ứng dụng.
4. Khi hoàn thành, ứng dụng nằm tại thư mục: **`dist\ScreenCamLite\ScreenCamLite.exe`**.

---

### Cách 2: Thực hiện thủ công bằng lệnh

1. **Cài đặt thư viện:**
   ```powershell
   pip install -r requirements.txt
   ```

2. **Chuẩn bị FFmpeg:**
   - Tạo thư mục `bin` (nếu chưa có).
   - Copy file `ffmpeg.exe` (Windows 64-bit) vào `bin/ffmpeg.exe`.

3. **Chạy thử từ mã nguồn:**
   ```powershell
   python app.py
   ```

4. **Đóng gói ra file `.exe`:**
   ```powershell
   pyinstaller ScreenCamLite.spec --noconfirm --clean
   ```
   File thực thi sẽ xuất hiện tại `dist\ScreenCamLite\ScreenCamLite.exe`.

---

## 🌟 Tính năng chính

- **Nguồn quay linh hoạt (Màn hình, Vùng chọn, Cửa sổ):**
  - **Full Màn hình:** Hỗ trợ từng màn hình riêng lẻ hoặc toàn bộ các màn hình ghép lại.
  - **Vẽ vùng tùy chỉnh (`📐 Vẽ vùng`):** Kéo chuột vẽ hình chữ nhật trên desktop, tự do di chuyển / co giãn trước khi quay. Tích hợp `👁️ Khung viền` di động trực tiếp trên màn hình (tự động ẩn khi bắt đầu quay để không lọt viền vào video).
  - **Quay theo Cửa sổ (`Cửa sổ: ...`):** Tự động nhận diện các ứng dụng đang mở, lấy đúng tọa độ và kích thước thực để quay khít ứng dụng mà không cần quay cả màn hình.
- **6 chế độ quay:**
  - **Màn hình + Cam nhỏ (`pip`):** Màn hình nền, Webcam lồng góc (4 góc, Bo góc / Tròn / Vuông, kích thước `12–45%`, viền sáng, lật gương).
  - **Cam + Màn hình nhỏ (`pip-screen`):** Webcam nền, Màn hình nhỏ lồng góc.
  - **Xếp Trên & Dưới (`stack-tb`):** Màn hình và Webcam xếp dọc không lồng đè; chọn thứ tự trên/dưới và tỷ lệ (`Khít 2 khung`, `9:16`, `16:9`).
  - **Song song 2 file (`dual`):** Xuất đồng thời 2 file MP4 riêng biệt (`_man-hinh.mp4` và `_webcam.mp4`).
  - **Chỉ Màn hình (`screen`)** và **Chỉ Webcam (`webcam`)**.
- **Âm thanh song song:** Thu đồng thời **Âm thanh hệ thống (WASAPI Loopback)** và **Microphone**, trộn kênh thời gian thực với thanh chỉnh âm lượng và VU meter.
- **Ghi thẳng MP4 (không cần chờ convert sau quay):** Xuất trực tiếp `.mp4` ngay trong lúc quay.
- **Tự cắt video định kỳ:** Hỗ trợ tự động cắt file sau mỗi `1 / 3 / 5 / 10 / 15 / 30` phút hoặc tùy chỉnh số phút.
- **Tối ưu CPU/RAM:** Xem trước nhẹ (~10 FPS), tùy chọn giữ Live Preview khi đang quay (`480×270 @ 6 FPS`), phím tắt toàn cục (`Ctrl+Shift+R`, `Ctrl+Shift+P`), Job Object tự dọn FFmpeg khi thoát, và ghi log xoay vòng tại `%APPDATA%\ScreenCamLite\logs\screencam.log`.
