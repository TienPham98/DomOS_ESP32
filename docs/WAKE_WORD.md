# Wake word nền và cá nhân hóa giọng DomOS

## Phần đã có trong code

- ESP32 tự mở kết nối giọng nói sau khi có Wi-Fi; WebSocket giữ reconnect.
- Chuyển app không đóng micro/session. Wake nhận được sẽ đưa Assistant lên trước.
- Các từ gọi: “Hey Dom”, “Hey”, “Dom”, chỉ ở đầu câu; “Heyday” và “Domino” không khớp.
- Google wake STT Việt/Anh chạy riêng khỏi provider STT câu lệnh. Timeout mỗi
  nhánh lấy từ `WAKE_STT_TIMEOUT_SEC` (mặc định 3 giây); chọn provider bằng
  `WAKE_STT_PROVIDER=google-web` hoặc `configured`.
- Câu gọi kèm lệnh mở Codex/MU được giữ đúng thứ tự: mở Assistant, thực hiện
  lệnh mở app đích, phát lời xác nhận; không kéo ngược về Assistant khi TTS chạy.
- Các task âm thanh vẫn giữ nguyên core, priority và frame PCM 60 ms.
- Worker tải dữ liệu Codex/MU dùng chung một cổng giới hạn đồng thời để không
  tranh hai stack internal RAM trong lúc voice chạy nền. App đang chờ thử lại
  sau 1 giây khi còn hiển thị; chu kỳ thường vẫn là Codex 60 giây, MU 1 giờ.
- `CONFIG_SPIRAM_TRY_ALLOCATE_WIFI_LWIP=y` cho các cấp phát Wi-Fi/LwIP phù hợp
  ưu tiên PSRAM. DMA và stack ghi LittleFS vẫn ở internal RAM; không chuyển
  stack task audio/fetch sang PSRAM. Không thay SSID/IP khi điều chỉnh bộ nhớ.
- Nghe nền hiện vẫn cần gateway và mạng; đây chưa phải nhận wake offline trên chip.
- Micro không được gửi khi loa TTS đang phát để tránh tự kích hoạt khi chưa có AEC.
  Bản này chưa có tính năng gọi wake để ngắt lời khi Assistant đang nói.

## Quyền riêng tư và giới hạn

Khi nghe nền, PCM từ micro được truyền tới gateway kể cả khi đang ở app khác.
Các đoạn được VAD chọn làm ứng viên wake có thể gửi tới dịch vụ STT cloud.
Không tự lưu WAV hay bật thu mẫu lâu dài. Chỉ tiến hành lưu bộ dữ liệu khi
người dùng bắt đầu một phiên thu mẫu có phạm vi và thời lượng rõ ràng.

“Hey” và “Dom” ngắn nên có thể bị kích hoạt bởi lời nói hoặc TV; “Hey Dom” là
câu gọi nên dùng để đánh giá độ ổn định. Thêm alias văn bản không huấn luyện
khả năng nhận giọng nhỏ, khác cao độ hoặc ở xa. Hiện chưa có model cá nhân hóa,
bộ WAV do chủ thiết bị cung cấp, hoặc kết quả đo độ chính xác trên bộ đó.

## Kế hoạch thu mẫu thực tế — chưa triển khai/thu tự động

Dùng chính micro ES8311 trên board, WAV PCM 16 kHz, 16-bit mono. Ghi nhãn
phrase, khoảng cách, giọng, môi trường và lượt thu; không dùng transcript cloud
làm nhãn đúng tự động vì chính transcript có thể nhận sai.

1. Mỗi câu “Hey Dom”, “Hey”, “Dom”: thu ở 0,5 m, 1 m, 2 m; giọng thường,
   nhỏ và lớn. Khởi đầu 5 lần cho mỗi tổ hợp (135 mẫu), điều chỉnh sau đánh giá.
2. Thu riêng câu không gọi trợ lý, từ gần giống và tiếng phòng/quạt/TV. Không
   chỉ thu mẫu đúng; phải đo được tỷ lệ kích hoạt nhầm.
3. Thu thêm một lượt kiểm thử độc lập. Không dùng các bản tăng/giảm âm lượng
   của cùng một bản gốc ở cả tập huấn luyện và tập kiểm thử.
4. Đánh giá từng khoảng cách/giọng: tỷ lệ gọi thành công, bỏ sót, kích hoạt
   nhầm mỗi giờ, độ trễ từ kết thúc wake tới trạng thái Listening (p50/p95).
5. Chỉ đưa model vào sử dụng nếu cải thiện trên bộ kiểm thử độc lập. Không
   hứa “nhận mọi tone và mọi khoảng cách” dựa trên vài mẫu hoặc test văn bản.

Để bỏ độ trễ STT cloud, bước tiếp theo là một wake detector cục bộ chuyên dụng,
không phải khôi phục LLM/Ollama local. Cần chọn target chạy trên gateway hoặc
ESP32 trước khi xây pipeline huấn luyện và xác minh ngân sách RAM/CPU.

- [openWakeWord](https://github.com/dscripka/openWakeWord): có huấn luyện model
  wake tùy chỉnh; [custom verifier](https://github.com/dscripka/openWakeWord/blob/main/docs/custom_verifier_models.md)
  là bộ lọc giọng thêm vào **model wake đã có**, không thay thế model gốc Hey Dom.
- [Espressif WakeNet](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/wake_word_engine/README.html):
  hướng chạy trên ESP32-S3; cần model phù hợp, không chỉ đổi một chuỗi ký tự.

## Áp dụng và kiểm tra

Build/flash firmware mới và restart Python Gateway để nạp code. Không đổi
Wi-Fi, địa chỉ trong `.env`, đường WebSocket hay pin board.

- Khởi động board, không mở Assistant; xác nhận kết nối voice được thiết lập.
- Đang ở Clock/Codex/MU, gọi lần lượt ba wake word và kiểm tra Assistant lên trước.
- “Hey Dom, kiểm tra Codex Credit”: kết quả cuối phải ở Codex, không quay lại Assistant.
- Đổi app và chờ TTS kết thúc: không được tự giành lại màn hình.
- Tắt/bật gateway hoặc mất/kết nối lại Wi-Fi: chỉ một voice session reconnect.
- Nói chuyện bình thường, bật TV: ghi nhận kích hoạt nhầm để điều chỉnh bằng dữ liệu.

Test tự động backend ở `backend-python/tests/test_background_wake.py`; kiểm tra
contract firmware ở `firmware/tests/test_firmware_contracts.py`. Các test mock
không chứng minh độ chính xác hoặc độ trễ micro trong phòng thật.
