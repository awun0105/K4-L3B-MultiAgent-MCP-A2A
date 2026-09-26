# L3B Multi-Agent MCP + A2A — Master Implementation Plan

Tài liệu này định hình toàn bộ kế hoạch kỹ thuật, kiến trúc hệ thống và lộ trình triển khai chi tiết cho dự án **Day09 L3B — Multi-Agent MCP + A2A Investigation System**.

---

## 1. Tổng quan & Mục tiêu Dự án

### 1.1 Mục tiêu
Xây dựng hệ thống Multi-Agent tự động điều tra và giải quyết khiếu nại thương mại điện tử (100 cases từ dataset Olist), kết nối với hệ thống **MCP Gateway** để thu thập chứng cứ được xác thực (`evidence_ref`), thực hiện giải quyết thực thể (Entity Resolution), phân tích vận chuyển, thanh toán, chính sách, hòa giải xung đột và xuất báo cáo đạt chuẩn JSON Schema cùng nhật ký quan sát (observable trace).

### 1.2 Phân bổ trọng số chấm điểm (Rubric)
* **Độ đúng nghiệp vụ (`semantic`) — 40%**: Nhận định đúng vấn đề cốt lõi (`primary_issue`), nguyên nhân gốc rễ, đối tượng chịu trách nhiệm và hành động xử lý.
* **Chất lượng bằng chứng (`evidence`) — 15%**: Bằng chứng liên quan chặt chẽ đến từng claim.
* **Evidence đúng MCP audit (`provenance`) — 15%**: Chỉ dùng `evidence_ref` hợp lệ từ server audit của case hiện tại, không dùng chéo case, không tự sinh ref.
* **Tính nhất quán giữa các trường (`consistency`) — 10%**: Số tiền, mã lỗi, đối tượng chịu trách nhiệm phải logic và đồng nhất.
* **Đúng JSON Schema (`schema`) — 5%**: Tuân thủ 100% `day09-l3b-output-v2` và `day09-trace-event-v1`.
* **Hiệu chuẩn độ tin cậy (`calibration`) — 5%**: Điểm `confidence` được tính toán hợp lý (0.0 đến 1.0).
* **Quy trình Multi-Agent trong Trace (`workflow`) — 5%**: Trace ghi lại đầy đủ và chuẩn xác các sự kiện A2A.
* **Hiệu quả gọi Tool (`efficiency`) — 5%**: Không gọi thừa tool, áp dụng caching, không spam query.

---

## 2. Kiến trúc & Công nghệ (Tech Stack)

### 2.1 Thành phần kỹ thuật
* **Ngôn ngữ & Runtime:** Python 3.11+.
* **LLM Engine:** Google Gemini Cloud API (chỉ dùng Cloud LLM):
  * **Model chính (High Reasoning):** `gemini-2.5-flash` hoặc `gemini-1.5-flash` (cho tốc độ cao, trích xuất chính xác, context window lớn).
  * **Model điều phối & phân xử:** `gemini-1.5-pro` hoặc `gemini-2.5-flash` (cho các logic suy luận phức tạp, hòa giải conflict).
  * **Cấu hình:** Đọc `GEMINI_API_KEY` từ `.env`.
* **Multi-Agent Orchestration:** **LangGraph** (`StateGraph`):
  * Quản lý trạng thái tập trung (`InvestigationState`).
  * Điều phối luồng xử lý A2A có điều kiện (conditional edges).
  * Tích hợp chặt chẽ việc ghi nhận sự kiện vào `TraceWriter`.
* **MCP Layer:** Thư viện `mcp`, `httpx2`, bọc thêm lớp **`CachedEvidenceGateway`** (In-memory per-case caching).
* **Validation Layer:** `jsonschema` (Draft 2020-12), thư viện `referencing` theo chuẩn starter kit.

### 2.2 Sơ đồ Luồng Hoạt động (State Graph Flow)

```text
[Input Case]
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

## 3. Thiết kế Chi tiết 6 Vai trò Agent

| Agent | Trách nhiệm chính | Tool MCP được phép gọi | Trace Events tạo ra |
| :--- | :--- | :--- | :--- |
| **`coordinator`** | Tiếp nhận case, lập kế hoạch điều tra, phân bổ tác vụ cho các agent chuyên môn, tổng hợp kết quả ban đầu. | Không gọi trực tiếp tool MCP dữ liệu. | `task_assigned`, `handoff` |
| **`entity_agent`** | Xếp hạng và phân tích các `candidate_order_ids`, tìm order ID chính xác, phân loại `resolved_order_ids` vs `rejected_candidates`, xác định `customer_unique_id`. | `get_customer_history`, `get_order` | `tool_result_consumed`, `handoff` |
| **`shipment_agent`** | Phân tích lộ trình vận chuyển, phát hiện nguyên nhân chậm trễ (`on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`), xác định seller chịu trách nhiệm. | `get_shipment_summary`, `get_sellers` | `tool_result_consumed`, `handoff` |
| **`payment_agent`** | Phân tích giao dịch, đối soát số tiền captured/refunded/refundable (BRL), phát hiện trùng lặp (`duplicate_capture`), lỗi thanh toán chia tách (`valid_split_payment`). | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `tool_result_consumed`, `handoff` |
| **`conflict_policy_agent`** | Tra cứu chính sách hiện hành (`policy_version`), so sánh dữ liệu giữa các nguồn, lập danh sách `data_conflicts`, đưa ra `policy_decided`. | `get_policy`, `get_product_context` | `tool_result_consumed`, `policy_decided`, `handoff` |
| **`verifier_agent`** | Lắp ráp cấu trúc `day09-l3b-output-v2`, kiểm định toàn vẹn qua JSON Schema Draft 2020-12, chốt confidence. | Không gọi MCP. | `verification_completed` |

---

## 4. Cơ chế Caching MCP & Quản trị Trace

### 4.1 Caching trong phạm vi Case (`CachedEvidenceGateway`)
* **Mục tiêu:** Đảm bảo không gọi 2 lần cùng một tool với cùng bộ tham số trong suốt chu kỳ giải quyết một case.
* **Cơ chế:**
  * Một cấu trúc `dict[tuple[str, str], dict]` lưu trữ cache nội bộ theo `(tool_name, json_sorted_args)`.
  * Khi Agent yêu cầu gọi tool:
    1. Kiểm tra cache trước.
    2. Nếu đã có: trả về dữ liệu cache ngay lập tức (không tốn HTTP request, không bị server phạt redundant call).
    3. Nếu chưa có: gọi server MCP, lưu kết quả hợp lệ vào cache, trả về cho Agent.
  * Khi Agent sử dụng kết quả, gọi `trace.emit(event_type="tool_result_consumed", evidence_refs=[...])`.
  * Bộ nhớ cache tự động được khởi tạo mới ở đầu mỗi case, triệt để loại bỏ nguy cơ rò rỉ `evidence_ref` chéo giữa các case.

### 4.2 Chuẩn mực Trace (`trace-event-v1`)
Các sự kiện phải tuân thủ nghiêm ngặt schema và quy tắc:
1. `case_received`: Ghi bởi CLI khi bắt đầu xử lý case.
2. `task_assigned`: Ghi khi `coordinator` giao nhiệm vụ cho agent chuyên biệt (thuộc tính: `actor="coordinator"`, `target="entity_agent"`,...).
3. `tool_result_consumed`: Ghi ngay khi một agent đọc dữ liệu từ MCP (phải kèm `tool_name` và `evidence_refs`).
4. `handoff`: Ghi khi một agent chuyển giao quyền hoặc kết quả điều tra sang agent tiếp theo.
5. `policy_decided`: Ghi bởi `conflict_policy_agent` khi quyết định áp dụng điều khoản chính sách giải quyết.
6. `verification_completed`: Ghi bởi `verifier_agent` sau khi kết quả vượt qua validation schema.
7. `case_finalized`: Ghi bởi CLI khi output được lưu an toàn xuống đĩa.

> **Cảnh báo an toàn:** Tuyệt đối không ghi prompt, không ghi nội dung suy luận nội bộ (chain-of-thought), không ghi token/API key vào `attributes` hay bất kỳ trường nào của trace.

---

## 5. Cấu trúc Trạng thái LangGraph (`InvestigationState`)

```python
class InvestigationState(TypedDict):
    case: dict[str, Any]
    case_id: str
    claims: list[dict[str, Any]]
    
    # Entity Resolution
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    customer_unique_id: str | None
    entity_confidence: float
    
    # Specialized Findings
    shipment_summary: dict[str, Any]
    payment_summary: dict[str, Any]
    policy_summary: dict[str, Any]
    product_summary: dict[str, Any]
    
    # Analysis & Conflicts
    data_conflicts: list[dict[str, Any]]
    claim_assessments: list[dict[str, Any]]
    root_cause: dict[str, Any]
    financial_resolution: dict[str, Any]
    resolution_actions: list[str]
    primary_issue: str
    secondary_issues: list[str]
    case_status: str
    confidence: float
    
    # Audit tracking
    collected_evidence_refs: list[str]
    final_output: dict[str, Any] | None
```

---

## 6. Lộ trình Triển khai Chi tiết (Phases & Steps)

### Giai đoạn 1: Môi trường & Thư viện (Environment & Dependencies)
* [ ] **Bước 1.1:** Bổ sung các thư viện cần thiết vào `pyproject.toml`:
  * `langgraph>=0.2.0`
  * `langchain-core>=0.3.0`
  * `google-genai>=1.0.0` (hoặc `langchain-google-genai`)
  * `pydantic>=2.7.0`
* [ ] **Bước 1.2:** Cập nhật file `.env.example` và hướng dẫn cấu hình `GEMINI_API_KEY`.
* [ ] **Bước 1.3:** Kiểm tra cài đặt dependencies trong môi trường Python 3.11.

### Giai đoạn 2: Lớp Caching MCP & Model Factory
* [ ] **Bước 2.1:** Tạo `src/student_agent/mcp_cache.py`:
  * Cài đặt `CachedEvidenceGateway` bọc quanh `EvidenceGateway`.
  * Quản lý cache an toàn theo case scope, thu thập tập hợp `evidence_refs` hợp lệ.
* [ ] **Bước 2.2:** Tạo `src/student_agent/llm_client.py`:
  * Khởi tạo Gemini Model Factory hỗ trợ structured JSON parsing.
  * Quản lý fallback & retry an toàn (exponential backoff cho rate limit).

### Giai đoạn 3: Triển khai 6 Subagents & Prompts Chuyên Biệt
* [ ] **Bước 3.1:** `entity_agent`:
  * LLM phân tích candidate kết hợp đối soát `get_customer_history` / `get_order`.
  * Trích xuất chính xác candidate cần reject và order ID được resolve.
* [ ] **Bước 3.2:** `shipment_agent`:
  * Phân tích `get_shipment_summary`, so khớp ngày dự kiến giao (`order_estimated_delivery_date`) với thực tế.
  * Phân loại: `on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`.
* [ ] **Bước 3.3:** `payment_agent`:
  * Phân tích `get_order_payments`, timeline thanh toán và hoàn tiền.
  * Tính tổng BRL (`captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`).
* [ ] **Bước 3.4:** `conflict_policy_agent`:
  * Gọi `get_policy`, đối chiếu chéo dữ liệu giữa đơn vị vận chuyển và thanh toán.
  * Xác định `data_conflicts`, kết luận `primary_issue` và giải pháp hoàn tiền (`financial_resolution`).
* [ ] **Bước 3.5:** `coordinator`:
  * Lập kế hoạch điều tra ban đầu và kích hoạt các agent chuyên biệt.
* [ ] **Bước 3.6:** `verifier_agent`:
  * Tổng hợp toàn bộ dữ liệu thành schema `day09-l3b-output-v2`.
  * Thực hiện validation qua `contracts.validate_output()`.

### Giai đoạn 4: Ghép nối LangGraph Workflow (`workflow.py`)
* [ ] **Bước 4.1:** Xây dựng `StateGraph` kết nối 6 agent với các luồng chuyển tiếp (edges).
* [ ] **Bước 4.2:** Đảm bảo toàn bộ trace events (`task_assigned`, `handoff`, `tool_result_consumed`, `policy_decided`, `verification_completed`) được phát sinh đầy đủ, đúng thứ tự.
* [ ] **Bước 4.3:** Cập nhật hàm `solve_case()` trong `src/student_agent/workflow.py` để thực thi đồ thị LangGraph.

### Giai đoạn 5: Cập nhật Tài liệu Kiến trúc (`ARCHITECTURE.md`)
* [ ] **Bước 5.1:** Điền đầy đủ bảng phân quyền Agent Ownership và Tool Permission.
* [ ] **Bước 5.2:** Ghi rõ cơ chế Entity Resolution, A2A Protocol, Evidence Lifecycle và Caching Policy.
* [ ] **Bước 5.3:** Cập nhật Failure Matrix và Reproducibility.

### Giai đoạn 6: Kiểm thử & Đóng gói (Testing & Packaging)
* [ ] **Bước 6.1:** Kiểm thử trên một case đơn lẻ (`L3B_CASE_001.json`).
* [ ] **Bước 6.2:** Chạy kiểm thử trên một tập 5 case mẫu để kiểm tra rate limit và schema integrity.
* [ ] **Bước 6.3:** Chạy toàn diện 100 case:
  ```bash
  day09 run
  day09 validate
  ```
* [ ] **Bước 6.4:** Đóng gói sản phẩm nộp bài:
  ```bash
  day09 package --output dist/submission.zip
  ```

---

## 7. Tiêu chuẩn Đạt yêu cầu (Definition of Done)
1. Tất cả 100 files `outputs/<case_id>.json` được tạo đầy đủ và vượt qua kiểm tra `day09 validate`.
2. Toàn bộ `traces/trace.jsonl` hợp lệ, không chứa secret key (`sk-team-...`) hay CoT, phản ánh chân thực chu trình làm việc của 6 agent.
3. 100% `evidence_refs` trong output tương ứng chính xác với các lần gọi MCP audit thực tế.
4. `ARCHITECTURE.md` được điền đầy đủ và đồng bộ hoàn hảo với source code.
5. Gói `dist/submission.zip` được tạo thành công với kích thước < 12 MB.
