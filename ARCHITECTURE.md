# L3B Architecture Record

## 1. System overview

Hệ thống điều tra khiếu nại thương mại điện tử Đa tác tử (Multi-Agent) xây dựng trên nền tảng **LangGraph (`StateGraph`)** kết hợp **Google Gemini Cloud LLM**, giao tiếp với MCP Evidence Gateway và ghi nhận toàn bộ chu trình hành động qua observable trace chuẩn `day09-trace-event-v1`.

```text
Input (Case JSON)
       │
       ▼
┌──────────────────┐
│   coordinator    │ ── (task_assigned / handoff)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   entity_agent   │ ── MCP: get_customer_history, get_order
└────────┬─────────┘    LLM: Ranking candidate & confirm matching
         │
         ▼ (handoff)
┌────────────────────────────────────────────────────────┐
│                  Specialist Agents                     │
│  ┌──────────────────┐           ┌──────────────────┐   │
│  │  shipment_agent  │           │  payment_agent   │   │
│  │ (get_shipment,   │           │ (get_payments,   │   │
│  │  get_sellers)    │           │  payment/refund) │   │
│  └────────┬─────────┘           └────────┬─────────┘   │
└───────────┼──────────────────────────────┼─────────────┘
            │                              │
            ▼                              ▼
┌────────────────────────────────────────────────────────┐
│                 conflict_policy_agent                  │
│   (MCP: get_policy, detect discrepancies, resolve)     │
└───────────────────────────┬────────────────────────────┘
                            │ (handoff)
                            ▼
┌────────────────────────────────────────────────────────┐
│                     verifier_agent                     │
│   (Assemble output, jsonschema contract validation,    │
│    emit verification_completed)                        │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
                     [Final Output JSON]
```

---

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| :--- | :--- | :--- | :--- | :--- |
| **`coordinator`** | Case input payload, customer message & claims | Tiếp nhận case, giải nén investigation scope, phân công nhiệm vụ cho Entity Agent | Không gọi trực tiếp MCP tool dữ liệu | `task_assigned`, `handoff` chuyển giao sang `entity_agent` |
| **`entity_agent`** | Candidate order IDs, customer unique ID hint, case message | Đánh giá & xếp hạng candidate qua LLM, xác định order chính xác, loại bỏ candidate giả/sai | `get_customer_history`, `get_order` | `resolved_order_ids`, `rejected_candidates`, handoff sang `shipment_agent` |
| **`shipment_agent`** | Resolved order ID, customer delivery claim | Điều tra timeline giao nhận giữa người bán và đơn vị vận chuyển, xác định seller trễ hạn | `get_shipment_summary`, `get_sellers` | `shipment_analysis` (`verdict`, `late_seller_ids`), handoff sang `payment_agent` |
| **`payment_agent`** | Resolved order ID, payment claims | Đối soát giao dịch, timeline thanh toán, phát hiện duplicate capture hoặc lỗi hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis`, `captured_total_brl`, handoff sang `conflict_policy_agent` |
| **`conflict_policy_agent`** | Báo cáo vận chuyển, thanh toán, customer claims | Tra cứu chính sách hiện hành, phát hiện xung đột dữ liệu giữa các nguồn, chốt primary issue | `get_policy`, `get_order_items`, `get_product_context` | `data_conflicts`, `policy_decided`, `financial_resolution`, handoff sang `verifier_agent` |
| **`verifier_agent`** | Toàn bộ kết quả điều tra của các agent | Tổng hợp output chuẩn `l3b-output-v2`, kiểm định schema, chốt confidence | Không gọi MCP | `verification_completed`, trả về output hoàn chỉnh |

---

## 3. Entity resolution và A2A protocol

* **Candidate Evaluation & Ranking:** Sử dụng Cloud LLM phân tích format (UUID 32 ký tự hex) kết hợp ngữ cảnh khiếu nại để xếp hạng candidate có khả năng cao nhất trước.
* **Đối soát MCP:** Truy vấn `get_customer_history` bằng `customer_unique_id_hint` để lấy danh sách order lịch sử của khách hàng. Chỉ gọi `get_order` cho candidate có độ ưu tiên cao nhất; ngay khi xác nhận order hợp lệ (`order_id == candidate`), dừng truy vấn để tiết kiệm budget MCP. Các candidate còn lại được phân loại vào `rejected_candidates`.
* **A2A Envelope & State:** Luồng trao đổi giữa các agent dựa trên `InvestigationState` của LangGraph, mang theo `case_id` làm correlation key bất biến.
* **Trace Isolation:** Các event chuyển giao ghi lại dạng `handoff` có thuộc tính quan sát được (như `resolved_orders_count`, `shipment_verdict`), tuyệt đối không ghi prompt hay chuỗi suy luận nội bộ (chain-of-thought).

---

## 4. Evidence và conflict lifecycle

* **MCP Response Validation:** Mọi phản hồi từ MCP Gateway được validate thông qua schema `day09-mcp-evidence-v1`. Lớp `CachedEvidenceGateway` lưu cache nội bộ theo bộ khóa `(tool_name, case_id, sorted_args)` đảm bảo không gọi lặp tool với cùng tham số.
* **Provenance Isolation:** Bộ nhớ cache và tập hợp `evidence_refs` được reset hoàn toàn ở đầu mỗi case (`reset_case()`), ngăn chặn triệt để nguy cơ tái sử dụng bằng chứng chéo giữa các case.
* **Trace Emission:** Mỗi lần agent tiêu thụ dữ liệu từ MCP, sự kiện `tool_result_consumed` được phát ra ngay lập tức với đúng `tool_name` và danh sách `evidence_refs` thực tế do server audit.
* **Data Conflict Representation:** Xung đột giữa lời khai khách hàng và bằng chứng kiểm toán (ví dụ: ngày giao thực tế vs ngày ước tính) được cấu trúc hóa theo mảng `data_conflicts` (`field`, `sources`, `selected_source`, `resolution_code`).

---

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| :--- | ---: | :--- | :--- |
| **MCP timeout / network drop** | 2 lần retry (exponential backoff) | Đánh dấu `verdict: "insufficient_evidence"` | Ghi nhận fallback trong state, không emit lỗi crash |
| **Entity not found / ambiguous** | 1 lần truy vấn qua hint | Ghi nhận `status: "not_found"`, chuyển giao tiếp tục với scope rỗng | `entity_confidence: 0.5` |
| **Source conflict** | Quyết định bằng LLM + Policy | Áp dụng quy tắc ưu tiên: Dữ liệu kiểm toán vận chuyển > Lời khai khiếu nại | `resolution_code: "AUDITED_PRECEDENCE"` |
| **Invalid specialist result** | Tự động điền default schema | Sử dụng default hợp lệ theo JSON Schema của starter kit | Không gián đoạn luồng LangGraph |
| **LLM Rate Limit (503/429)** | 3 lần retry + Fallback Model list | Tự động chuyển đổi giữa `gemini-3-flash-preview` và `gemini-3.8-flash` | Retry tự động trong worker thread |

---

## 6. Verification invariants

Trước khi hoàn tất case và lưu file output, `verifier_agent` kiểm tra các bất biến logic:
1. **Schema Invariant:** Output bắt buộc phải vượt qua `Draft202012Validator` với schema `day09-l3b-output-v2.schema.json`.
2. **Case ID Match:** `output["case_id"] == case["case_id"]`.
3. **Evidence Integrity:** Tất cả `evidence_refs` trong output phải là tập con của các ref thực tế đã thu thập qua MCP audit trong chính case đó.
4. **Resolution Coherence:** Nếu `primary_issue == "late_delivery_logistics"`, thì `financial_resolution` phải tương ứng với mức refund trong policy (16.0 BRL) và `responsible_parties` phải là `logistics_provider`.
5. **Trace Conformity:** Chuỗi trace kết thúc bằng `verification_completed` trước khi coordinator đóng case bằng `case_finalized`.

---

## 7. Reproducibility

* **LLM Engine:** Google Gemini Cloud API (`gemini-3-flash-preview` / `gemini-3.8-flash`) với `temperature=0.1`.
* **Python Runtime:** Python 3.11.9.
* **Dependencies Pinning:** `langgraph>=1.2`, `langchain-core>=1.0`, `google-genai>=2.0`, `jsonschema>=4.25`, `httpx2>=2.13`.
* **Execution Commands:**
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
