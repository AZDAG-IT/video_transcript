# Tích hợp vtranscript vào pipeline qua dòng lệnh

Tài liệu cho người gắn `vtranscript` vào một pipeline có sẵn (cron, hàng đợi job, CI, script xử lý hàng loạt). Pipeline chỉ cần gọi lệnh, chờ mã thoát, rồi đọc file kết quả. Không cần import Python.

## 1. Cài đặt trên máy chạy pipeline

Cần Python 3.10 trở lên. Không cần GPU và không cần cài ffmpeg riêng (gói `imageio-ffmpeg` mang theo).

```bash
uv tool install git+https://github.com/AZDAG-IT/video_transcript   # hoặc: pipx install git+...
mkdir -p ~/.config/vtranscript
curl -fsSL https://raw.githubusercontent.com/AZDAG-IT/video_transcript/main/.env.example \
  > ~/.config/vtranscript/.env && chmod 600 ~/.config/vtranscript/.env
# sửa ~/.config/vtranscript/.env: điền OPENROUTER_API_KEY
```

Sau bước này có 3 lệnh dùng được ở mọi thư mục:

| Lệnh | Việc |
|---|---|
| `vtranscript` | Xử lý một video |
| `vtranscript-sotay` | Xem, xóa mục, quay lại bản trước của sổ tay tự học |
| `vtranscript-bench` | Xem số dư key, so chất lượng giữa các lần chạy |

Muốn cập nhật bản mới thì chạy `uv tool upgrade vtranscript`, hoặc cài lại bằng lệnh `install` ở trên kèm `--force`.

## 2. Cấu hình

Thứ tự ưu tiên: tham số dòng lệnh > biến môi trường > `--profile` > file `.env` > mặc định trong code.

Trên máy chủ, cách gọn nhất là **đặt biến môi trường cho job** thay vì sửa file:

| Biến | Ý nghĩa | Gợi ý cho pipeline |
|---|---|---|
| `OPENROUTER_API_KEY` | Key gọi ASR và LLM | Lấy từ kho secret của pipeline, không ghi vào log |
| `VT_ENV_FILE` | Đường dẫn file `.env` | Dùng khi không muốn đặt ở `~/.config/vtranscript/.env` |
| `VT_KNOWLEDGE_DIR` | Thư mục sổ tay tự học | **Một thư mục cố định, dùng chung cho mọi job** (xem mục 5) |
| `VT_LLM_MODEL`, `VT_LLM_STRONG_MODEL`, `VT_LLM_VISION_MODEL`, `VT_ASR_MODEL` | Đổi model | Để trống thì dùng bộ mặc định (chất lượng tốt nhất đã đo) |

Muốn chọn cả bộ model một lần thì dùng `--profile best | budget | cheap`.

## 3. Lệnh gọi chuẩn

```bash
vtranscript "/data/in/video.mp4" -o "/data/out/<job_id>" --skip-video
```

- `-o`: thư mục kết quả riêng cho mỗi job. Mặc định là `output/<tên video>` trong thư mục hiện tại, nên hai video trùng tên sẽ đè nhau.
- `--skip-video`: không xuất video có phụ đề in cứng. Đây là bước chậm nhất. Bỏ cờ này nếu pipeline cần file `04_video_phu_de.mp4`.
- `--glossary FILE`: từ điển tên riêng của khách hoặc dự án (định dạng xem `glossary.example.txt`). Đây là cách chắc nhất để tên người và tên sản phẩm đúng ngay từ video đầu tiên.
- `--no-summary-images`: tóm tắt không chèn ảnh, chỉ có chữ.
- `--lang en`: video không phải tiếng Việt.
- `--workers N`: số lượt gọi LLM song song, mặc định 4.

Xem đủ tùy chọn bằng `vtranscript --help`.

### Mã thoát

| Mã | Ý nghĩa | Pipeline nên làm gì |
|---|---|---|
| `0` | Xong | Đọc kết quả |
| `2` | Lỗi đầu vào: không thấy file, video không có tiếng, cấu hình sai, thiếu thư viện | Đánh dấu job hỏng, **không thử lại** |
| khác 0 (thường là `1`) | Lỗi giữa chừng: mạng, API, hết hạn mức key | **Thử lại đúng lệnh cũ** (mục 4) |

Log tiến trình in ra stderr, có dòng `Chi phí lần chạy này (ước tính): $…` ở cuối. Mỗi lần chạy cũng ghi thêm một dòng vào `_work/chi_phi.jsonl` (model, số lượt gọi, số tiền). Pipeline đọc file này để tổng hợp chi phí.

## 4. Chạy lại an toàn (idempotent)

Mỗi bước (audio → khung hình → ASR → chuẩn hóa → tóm tắt → video) lưu kết quả vào `_work/`, và mỗi lượt gọi LLM được cache theo nội dung. Gọi lại **đúng lệnh cũ, cùng `-o`** thì:

- bước đã xong được bỏ qua, không tốn tiền lại;
- job bị ngắt giữa chừng chạy tiếp từ chỗ dở.

Muốn làm lại từ một bước thì thêm `--redo <bước>`, với bước là `audio | visual | asr | normalize | summary | video`. Ví dụ `--redo normalize` chạy lại chuẩn hóa, tóm tắt, video nhưng giữ ASR.

Sau khi đã lấy kết quả, có thể xóa `_work/` cho nhẹ ổ đĩa. Thư mục này chứa audio và khung hình, chiếm dung lượng lớn nhất. Xóa thì mất khả năng chạy tiếp hoặc chạy lại không tốn tiền.

## 5. Sổ tay tự học khi chạy trong pipeline

### Có học ở mọi lần chạy không?

**Có, mặc định là có.** Mỗi lần bước chuẩn hóa thật sự chạy, sau khi sửa xong video, tool:

1. đề xuất mục cần học bằng cách so bản thô với bản đã sửa;
2. gọi một model rẻ duyệt từng mục, khoảng $0.002;
3. ghi các mục được duyệt vào kho.

Một vài trường hợp đặc biệt:

- **Chạy lại một video đã có kết quả chuẩn hóa** thì không học lại, vì bước chuẩn hóa bị bỏ qua (mục 4).
- **Cùng một video chạy lại ở thư mục khác** cũng không bị đếm 2 lần. Video được nhận diện theo nội dung transcript gốc, không theo tên file. Lần học sau chỉ thay bằng chứng cũ của video đó.
- **Mục mới học chưa được dùng ngay.** Một mục chỉ được đưa thẳng vào bước sửa khi đã gặp ở ít nhất 3 video, hoặc ít nhất 2 video có căn cứ chắc (thấy trên màn hình). Trước đó mục chỉ là gợi ý, và chỉ áp cho video cùng lĩnh vực.

### Khi nào nên tắt học

| Tình huống | Cờ |
|---|---|
| Chạy thử, thử model rẻ hơn, chạy kiểm thử CI | `--no-learn` (vẫn dùng sổ tay, không ghi) |
| Video âm thanh rất kém, video không đại diện cho nội dung thật | `--no-learn` |
| Muốn kết quả hoàn toàn độc lập với sổ tay | `--no-knowledge` (không đọc, không ghi) |

Kiểm thử CI nên dùng thêm một kho riêng (`--knowledge /tmp/kho_test`) để không đụng vào kho thật.

### Một kho dùng chung cho cả team

- Đặt `VT_KNOWLEDGE_DIR` trỏ tới cùng một thư mục cho mọi job, để video của ai chạy cũng góp vào một sổ tay.
- Nhiều job chạy song song **trên cùng một máy** thì an toàn: lúc ghi, tool khóa kho bằng file `.khoa` (flock).
- Nhiều máy cùng ghi vào một thư mục mạng (NFS, SMB) thì flock không đảm bảo, và trên Windows tool không khóa. Trường hợp này nên chọn một trong hai cách:
  - chỉ một máy được học, các máy khác chạy `--no-learn`;
  - mỗi máy ghi vào kho riêng.
- Kho là các file JSON nhỏ, có thể sao lưu bằng cách chép cả thư mục. Trước mỗi lần ghi, tool tự chụp lại kho vào `lich_su/` và giữ 20 bản gần nhất.

### Theo dõi và sửa khi học sai

```bash
vtranscript-sotay xem                 # tóm tắt kho; chi tiết đọc file SO_TAY.md trong thư mục kho
vtranscript-sotay xoa "từ sai"        # xóa mục và chặn không học lại
vtranscript-sotay quay-lai            # trả kho về ảnh chụp gần nhất (hoặc chỉ tên bản trong lich_su/)
```

Mục bị xóa sẽ bị chặn, không học lại nữa. Nên cho một người trong team đọc `SO_TAY.md` định kỳ, ví dụ mỗi tuần. Mỗi lần chạy có thêm file `_work/normalize/so_tay_hoc.json`, ghi rõ video đó đề xuất gì, học gì, bỏ gì và vì sao.

### Luật của team

Luật cố định theo thể loại nằm trong `vtranscript/the_loai/<thể loại>.md`. Máy không bao giờ tự sửa các file này. Gặp thể loại mới, máy tự đặt mã và liệt kê trong `SO_TAY.md` với ghi chú "chưa có luật của team". Muốn thêm luật thì viết file `.md` mới cùng tên mã rồi cài lại gói.

## 6. Đọc kết quả

Các file trong thư mục `-o`:

| File | Dùng cho |
|---|---|
| `02_transcript_chuan_hoa.json` | **File chính cho máy đọc** (cấu trúc bên dưới) |
| `02_transcript_chuan_hoa.txt` / `.srt` | Transcript đã chuẩn hóa, dạng chữ / phụ đề |
| `01_transcript_goc.txt` / `.srt` | Bản thô từ ASR, chưa sửa |
| `02_bao_cao_chuan_hoa.md` | Báo cáo cho người duyệt: chỗ cần kiểm tra lại, từng thay đổi |
| `03_tom_tat.md` (+ `03_tom_tat_hinh/`) | Tóm tắt nội dung, có mốc thời gian |
| `04_video_phu_de.mp4` | Video in phụ đề (không có nếu dùng `--skip-video`) |
| `_work/normalized.json` | Dữ liệu đầy đủ của bước chuẩn hóa, gồm khối `so_tay` (thể loại, lĩnh vực, giọng, gợi ý đã dùng) |

Cấu trúc `02_transcript_chuan_hoa.json`:

```json
{
  "video": "video.mp4",
  "duration": 312.4,
  "glossary": {"entries": [{"canonical": "Kubernetes", "variants": ["cu bơ nét"], "...": "..."}]},
  "segments": [
    {"id": 0, "start": 0.0, "end": 4.2, "text": "câu đã chuẩn hóa", "raw": "câu thô từ ASR"}
  ]
}
```

Thể loại và lĩnh vực máy nhận ra (ví dụ để gắn nhãn hoặc định tuyến video) nằm ở `_work/normalized.json` → `so_tay.the_loai`, `so_tay.linh_vuc`, `so_tay.giong`.

## 7. Thời gian và chi phí

Với bộ model mặc định, qua OpenRouter:

| | Video ngắn (5–10 phút) | Video 1 giờ |
|---|---|---|
| Chi phí | khoảng $0.10 | khoảng $1 |
| Phần sổ tay (phân tích + duyệt) | khoảng $0.004 | khoảng $0.005 |

Thời gian chủ yếu phụ thuộc API: video ngắn khoảng 2 phút, chưa tính in phụ đề. Bước in phụ đề tốn CPU, tỉ lệ với độ dài video.

Key OpenRouter nên đặt giới hạn chi tiêu theo ngày. Khi hết hạn mức, lệnh thoát với mã khác 0, và chạy lại sau khi hạn mức được làm mới sẽ tiếp tục từ chỗ dở (mục 4).

Số dư key: `vtranscript-bench balance`.

## 8. Ví dụ

### Script xử lý một thư mục

```bash
#!/usr/bin/env bash
set -u
export VT_KNOWLEDGE_DIR=/srv/vtranscript/kien_thuc
IN=/data/in; OUT=/data/out
for f in "$IN"/*.mp4; do
  name=$(basename "$f" .mp4)
  [ -f "$OUT/$name/02_transcript_chuan_hoa.json" ] && continue      # đã xong
  for try in 1 2 3; do
    vtranscript "$f" -o "$OUT/$name" --skip-video 2> "$OUT/$name.log"; rc=$?
    [ $rc -eq 0 ] && break
    [ $rc -eq 2 ] && { echo "$name: lỗi đầu vào, bỏ qua"; break; }
    sleep 60                                                        # lỗi mạng/API: chờ rồi chạy tiếp
  done
done
```

### Gọi từ Python (worker hàng đợi)

```python
import json, subprocess
from pathlib import Path

def xu_ly(video: str, out_dir: str, hoc: bool = True) -> dict:
    cmd = ["vtranscript", video, "-o", out_dir, "--skip-video"]
    if not hoc:
        cmd.append("--no-learn")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 2:
        raise ValueError(r.stderr.strip()[-500:])      # lỗi đầu vào: không thử lại
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-500:])    # lỗi tạm thời: cho hàng đợi thử lại
    return json.loads((Path(out_dir) / "02_transcript_chuan_hoa.json").read_text(encoding="utf-8"))
```
