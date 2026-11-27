# ScreenCam Lite

Ứng dụng quay màn hình, webcam và âm thanh song song siêu nhẹ cho Windows (Python + CustomTkinter + FFmpeg), hỗ trợ mã hóa phần cứng Intel Quick Sync (`h264_qsv`) / MediaFoundation (`h264_mf`) / `libx264`.

## Tính năng chính

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

## Cài đặt & Chạy từ source code

1. Cài Python 3.9+ (64-bit) trên Windows và các thư viện:
   ```powershell
   pip install -r requirements.txt
   ```
2. Đặt `ffmpeg.exe` (Windows x64) vào `bin/ffmpeg.exe` (hoặc cài `ffmpeg` vào `PATH`).
3. Chạy ứng dụng:
   ```powershell
   python app.py
   ```

## Đóng gói thành `.exe` độc lập

```powershell
pyinstaller --noconfirm --onedir --windowed --name ScreenCamLite --icon assets\icon.ico --add-data "assets;assets" --add-data "bin;bin" --collect-data customtkinter app.py
```

Kết quả build nằm tại `dist\ScreenCamLite\ScreenCamLite.exe`.
