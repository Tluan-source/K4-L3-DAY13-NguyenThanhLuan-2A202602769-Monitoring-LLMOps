# Template Alert và Runbook

Mỗi alert phải dựa trên triệu chứng người dùng hoặc SLO, không dựa trực tiếp vào tên implementation nội bộ.

## Alert mẫu để tham khảo

Ví dụ dưới đây minh họa mức độ cụ thể cần có. Học viên không cần copy nguyên, nhưng ba alert trong bài nộp nên rõ ràng tương tự: điều kiện là gì, kéo dài bao lâu, ảnh hưởng tới user ra sao và người trực cần kiểm tra gì trước.

- Tên: `HighLatencyP95`
- Severity: `warning`
- Duration: `5m`
- Kênh thông báo: Slack `#k4-l3b-alerts`
- SLI/SLO liên quan: latency P95 của `response_sent.latency_ms`
- Điều kiện và thời gian duy trì: `p95(latency_ms) > 3000ms` trong 5 phút
- Ảnh hưởng tới người dùng: người dùng phải chờ lâu hơn trước khi nhận câu trả lời
- Ba bước kiểm tra đầu tiên:
  1. Mở dashboard latency để xác nhận P95/P99 và khoảng thời gian tăng.
  2. Lọc `data/logs.jsonl` trong khoảng đó, lấy một `correlation_id` có `latency_ms` cao.
  3. Mở trace cùng `correlation_id` trên Langfuse, so sánh các span chính để xác định bước nào bất thường.
- Mitigation tạm thời: dựa trên evidence thực tế để rollback prompt, khôi phục cấu hình liên quan, tắt practice scenario hoặc giảm tải khi demo.
- Owner: `student-2A202602769`

## Alert 1

- Tên: `HighLatencyP95`
- Severity: `warning`
- Duration: `5m`
- Kênh thông báo: Slack `#k4-l3b-alerts`
- SLI/SLO liên quan: SLO `fast_successful_requests` (good event = `response_sent` và `latency_ms <= 3000`); panel latency P95
- Điều kiện và thời gian duy trì: `p95(response_sent.latency_ms) > 3000ms` liên tục trong 5 phút
- Ảnh hưởng tới người dùng: khoảng 5% request chậm nhất phải chờ hơn 3 giây mới nhận câu trả lời; cảm giác hệ thống đơ
- Ba bước kiểm tra đầu tiên:
  1. Mở dashboard panel **Latency percentiles and TTFT**, xác nhận P95/P99 và TTFT P95 trong cửa sổ 60 phút, ghi khoảng thời gian đường P95 vượt 3000ms.
  2. Lọc `data/logs.jsonl` event `response_sent` cùng khoảng thời gian, lấy một `correlation_id` có `latency_ms` cao nhất.
  3. Mở trace cùng `correlation_id` trên Langfuse, so sánh duration **retrieval** với **generation** để khoanh bước gây chậm.
- Mitigation tạm thời: tắt practice scenario latency/RAG nếu đang bật; rollback label `production` về prompt `baseline` nếu vừa promote; giảm concurrency load test khi demo.
- Owner: `student-2A202602769`

## Alert 2

- Tên: `HighErrorRate`
- Severity: `critical`
- Duration: `5m`
- Kênh thông báo: Slack `#k4-l3b-alerts`
- SLI/SLO liên quan: guardrail `error_rate_pct_max: 2`; panel Errors; SLO total_event = `request_received`
- Điều kiện và thời gian duy trì: `count(request_failed) / count(request_received) * 100 > 2` liên tục trong 5 phút
- Ảnh hưởng tới người dùng: hơn 2% request không nhận được câu trả lời (HTTP 500 / lỗi agent); tính năng chat gián đoạn
- Ba bước kiểm tra đầu tiên:
  1. Mở dashboard panel **Error rate and retrieval success**, xác nhận error rate và breakdown `error_type`.
  2. Lọc `data/logs.jsonl` event `request_failed`, ghi `error_type`, `correlation_id`, `ts`.
  3. Mở trace cùng `correlation_id`; nếu không có generation thì lỗi xảy ra trước LLM (retrieval/config); nếu có span ERROR thì đọc status message.
- Mitigation tạm thời: tắt incident `llm_error` / practice đang inject; kiểm tra `.env` Langfuse và restart API; thông báo user thử lại sau khi error rate hạ dưới 2%.
- Owner: `student-2A202602769`

## Alert 3

- Tên: `LowRetrievalSuccess`
- Severity: `warning`
- Duration: `5m`
- Kênh thông báo: Slack `#k4-l3b-alerts`
- SLI/SLO liên quan: guardrail `retrieval_success_rate_pct_min: 90`; panel Errors (retrieval success)
- Điều kiện và thời gian duy trì: `count(tool_success == true) / count(tool_success != null) * 100 < 90` liên tục trong 5 phút
- Ảnh hưởng tới người dùng: câu trả lời thiếu context / sai vì RAG fail; chất lượng tụt dù LLM vẫn chạy
- Ba bước kiểm tra đầu tiên:
  1. Mở dashboard panel **Error rate and retrieval success**, xác nhận retrieval success rate dưới 90%.
  2. Lọc log `tool_name=retrieval` và `tool_success=false`, lấy `correlation_id`.
  3. Mở trace cùng ID, xem span **retrieval** (duration, level ERROR) rồi đối chiếu generation có docs rỗng không.
- Mitigation tạm thời: tắt practice `rag_fail` / `rag_slow`; xác nhận `docs/knowledge.md` đọc được; rollback prompt nếu vừa đổi format khiến retrieval bị hiểu sai; không scale traffic cho đến khi success rate ≥ 90%.
- Owner: `student-2A202602769`
