# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Minh Thắng  **MSSV:** 2A202602706  **Ngày:** 05-10-2026

Kết quả được sinh bởi `python bench_kg.py --judge` với Gemini 3.5 Flash-Lite, Gemini Embedding 001, `top_k=3`, `chunk_size=800`. Corpus tạo 176 chunks; graph đầy đủ có 264 nodes và 460 relationships.

## 1. Chi phí

```text
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    106.2
graph       196     34779     5390   0.02391    299.6

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.33      696       75   0.00040     8.47
graph       1.00   2.00     2795      241   0.00144     8.63
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | ---: | ---: | ---: |
| Indexing USD | $0.00000* | $0.02391 | Không xác định* |
| Indexing giây | 106.2 | 299.6 | 2.82× |
| Mỗi câu: USD | $0.00040 | $0.00144 | 3.60× |
| Mỗi câu: giây | 8.47 | 8.63 | 1.02× |
| Mỗi câu: input tokens | 696 | 2,795 | 4.02× |

\* Gemini OpenAI-compatible embedding response trong lần đo không trả usage token, vì vậy code ghi chi phí embedding của Flat bằng 0. Đây là giới hạn phép đo, không có nghĩa embedding thực sự miễn phí.

Graph indexing gọi thêm 20 lần LLM để trích xuất 20 bài báo: phần tăng thêm là 34.779 input tokens, 5.390 output tokens và khoảng 193,4 giây so với Flat. Khi query, graph facts làm input dài hơn khoảng 4 lần nhưng độ trễ trung bình chỉ tăng 0,16 giây; chi phí tăng thêm là khoảng $0,00104/câu. Nếu chỉ xét USD đo được, chi phí dựng KG $0,02391 tương đương khoảng 23 lần chênh lệch chi phí query.

## 2. Từng câu hỏi

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao |
| --- | --- | ---: | ---: | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Đáp án nằm trọn trong một chunk Điều 2; graph chỉ bổ sung số Khoản. |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Hai tên bị cáo cùng nằm trong một đoạn báo chí; chưa cần multi-hop. |
| Q3 | cross-kb | 0.33 / 1 | 1.00 / 2 | Graph | Flat có người, tội, án nhưng thiếu Điều 251 và khung Khoản 1; graph nối qua `Crime`. |
| Q4 | cross-kb | 0.33 / 1 | 1.00 / 2 | Graph | Entity fallback nhận alias “Hoàng Nato”, rồi lấy Khoản 4 Điều 255 và mức tối đa chung thân. |
| Q5 | cross-kb-multi-hop | 0.40 / 1 | 1.00 / 2 | Graph | Graph kết hợp `Case → Substance.amount_grams` với `Clause → Substance` để chọn Khoản 4 Điều 250. |
| Q6 | aggregation | 0.00 / 1 | 1.00 / 2 | Graph | Flat top-3 chỉ thấy ba đoạn rời và không nêu đúng tên vụ; graph đi ngược từ node `MDMA` để gom nhiều Case. |

Quy luật thấy rõ: Flat đủ tốt cho câu single-hop (Q1–Q2), còn Graph thắng cả bốn câu cần nối KB hoặc tổng hợp (Q3–Q6). Trung bình Graph tăng recall từ 0,51 lên 1,00 và judge từ 1,33 lên 2,00.

## 3. Phân tích lỗi

### Lỗi E5: aggregation có recall cao nhưng lẫn vụ trùng/ngoài danh sách chuẩn

- **Hiện tượng:** Q6 Graph đạt recall 1,00 và judge 2 nhưng trả 5 mục, trong khi gold gom thành 3 vụ chính. Vụ Cái Quang Huy xuất hiện dưới hai tên; một bản sao mang `doc_id=news-100260918080821054`, vốn là bài chính về Lê Minh Thành nhưng cuối file có đoạn giới thiệu bài liên quan Cái Quang Huy.
- **Bằng chứng:**

```cypher
MATCH (k:Case)-[r:SEIZED]->(:Substance {name:'MDMA'})
RETURN k.name AS case_name, k.doc_id AS doc_id, r.amount AS amount
ORDER BY doc_id, case_name;
```

```text
Vụ vận chuyển hơn 10kg ... Cái Quang Huy | news-100260917203001265 | hơn 9,6kg
Vụ mua bán ... Lê Minh Thành            | news-100260918080821054 | 5 viên
Vụ vận chuyển ... Cái Quang Huy         | news-100260918080821054 | hơn 9,6kg  <-- bản sao
Vụ bắt Hoàng Nato ...                   | news-100260920221957595 | khoảng 100g...
Vụ án tại Viện Pháp y tâm thần ...      | news-100260924105118645 | rỗng
Vụ tổ chức sử dụng ... Pháp y tâm thần  | news-100260930085028036 | 0,686g
```

- **Nguyên nhân:** news corpus có teaser/nội dung bài liên quan nối ở cuối một số file. Prompt cho phép trả nhiều `cases`, nên LLM trích cả bài chính lẫn teaser thành Case độc lập. Khóa `Case.id = doc_id#case-n` chống va chạm kỹ thuật nhưng không hợp nhất hai Case cùng sự kiện ở hai tài liệu.
- **Đề xuất sửa:** cắt bài tại marker “Tin liên quan” hoặc phát hiện đổi chủ đề trước extraction; thêm `event_fingerprint` từ người chính + ngày + địa điểm + tội danh để nối `SAME_EVENT_AS`; khi aggregation, group các Case tương tự và chỉ hiển thị một nhãn chuẩn. Nên bổ sung precision/F1 thay vì chỉ keyword recall.

### Lỗi E4: phép đo bỏ sót chi phí embedding và judge không phạt thông tin thừa

- **Hiện tượng:** bảng indexing ghi Flat gọi 176 lần nhưng `in_tok=0`, `USD=0`; do đó không thể tính hợp lệ tỷ lệ chi phí Graph/Flat lúc dựng hệ thống. Đồng thời Q6 Graph được judge 2 dù có nhiều mục hơn gold và có bản sao sự kiện.
- **Bằng chứng:**

```text
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    106.2
graph       196     34779     5390   0.02391    299.6

Q6 graph: recall=1.00, judge=2; câu trả lời liệt kê 5 mục trong khi gold có 3 vụ chính.
```

- **Nguyên nhân:** Gemini OpenAI-compatible embedding endpoint không cung cấp usage trong response hiện tại và bảng giá trong code không định giá `gemini-embedding-001`, nên meter cộng 0. Judge prompt chỉ chấm “đúng và đủ ý chính”, không có tiêu chí phạt duplicate hoặc thông tin dư.
- **Đề xuất sửa:** đếm input embedding bằng tokenizer/usage riêng của provider và cấu hình giá embedding có ngày hiệu lực; báo cả chi phí tuyệt đối lẫn “unmetered”. Với judge, thêm các trường `precision`, `unsupported_claims`, `duplicates`, đồng thời đối chiếu Q6 trực tiếp bằng tập `doc_id` từ Cypher.

## 4. Kết luận

Flat RAG nên được ưu tiên khi đáp án nằm gọn trong một nguồn hoặc một chunk: Q1 và Q2 đều đạt recall 1,00, judge 2 với chi phí query thấp hơn. KG đáng tiền khi câu hỏi cần nối người/vụ án trong báo chí với Điều/Khoản trong luật, suy ra Khoản từ định lượng, hoặc tổng hợp xuyên tài liệu: Q3–Q6 đều tăng lên recall 1,00 và judge 2, trong khi Flat chỉ đạt recall 0,33; 0,33; 0,40; 0,00.

Đổi lại, Graph tốn $0,02391 và thêm khoảng 193 giây khi dựng, input/câu cao hơn 4,02×, chi phí/câu cao hơn 3,60×. Với corpus ít thay đổi và có nhiều truy vấn cross-KB, chi phí dựng một lần là hợp lý; với dữ liệu thay đổi liên tục hoặc chỉ hỏi single-hop, Flat đơn giản và kinh tế hơn. Mốc tham khảo theo chi phí đo được là khoảng 23 câu hỏi để phần chi phí dựng KG bằng tổng chênh lệch chi phí query, nhưng quyết định thực tế phải dựa thêm vào giá trị của độ chính xác tăng thêm.

## 5. Tự kiểm

```text
$ pytest tests/ -q
................................................                         [100%]
48 passed
```

```text
$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 195 node / 344 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 27 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00247.
```

Ảnh Neo4j cần nộp: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`. Người chọn cho `kg_my_case.png`: Cái Quang Huy. Dùng lần lượt ba truy vấn sau trong Neo4j Browser:

```cypher
MATCH (n)
RETURN labels(n)[0] AS label, count(*) AS n
ORDER BY n DESC;
```

```cypher
MATCH path=(person:Person {name:'Lê Minh Thành'})-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(article:Article)
RETURN path;
```

```cypher
MATCH path=(person:Person {name:'Cái Quang Huy'})-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(article:Article)-[:HAS_CLAUSE]->(:Clause {number:4})
RETURN path;
```

## Vấn đề gặp phải

- Python ban đầu lỗi `CERTIFICATE_VERIFY_FAILED` khi gọi Gemini. Đã thêm `truststore` để dùng Windows certificate store mà vẫn giữ xác minh TLS.
- Gemini API báo `gemini-2.5-flash-lite` không còn cấp cho người dùng mới. Đã cập nhật sang `gemini-3.5-flash-lite` và giá token tương ứng trong `src/llm.py`.
- Chi phí embedding Gemini chưa đo được do response không trả token usage; báo cáo giữ số gốc và đánh dấu giới hạn thay vì suy đoán.
