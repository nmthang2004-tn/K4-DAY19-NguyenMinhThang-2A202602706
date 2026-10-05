"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts
    GraphRAGAgent.answer(question, top_k)         -> str

This implementation uses a person-level ontology so different defendants in one case do not
inherit one another's charges or verdicts. Crime and Substance are cross-KB bridge nodes.

Implemented ontology:

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:THRESHOLDS_FOR]->(:Substance)
    (:Clause)-[:DEFINES_PENALTY]->(:PenaltyRange)
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:SEIZED {amount, amount_grams}]->(:Substance)
    (:Case)-[:OCCURRED_IN]->(:Location {name})
    (:Person {name, aliases, doc_id})-[:DEFENDANT_IN {role}]->(:Case)
    (:Person)-[:CHARGED_WITH]->(:Crime)
    (:Person)-[:RECEIVED]->(:Verdict {text, years, months, is_life, is_death, doc_id})
"""

from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections.abc import Collection
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

CRIME_SYNONYMS = {
    "mua bán ma túy": "mua bán trái phép chất ma túy",
    "buôn bán ma túy": "mua bán trái phép chất ma túy",
    "vận chuyển ma túy": "vận chuyển trái phép chất ma túy",
    "tàng trữ ma túy": "tàng trữ trái phép chất ma túy",
    "sử dụng ma túy": "sử dụng trái phép chất ma túy",
    "chứa chấp sử dụng ma túy": "chứa chấp việc sử dụng trái phép chất ma túy",
    "tổ chức sử dụng ma túy": "tổ chức sử dụng trái phép chất ma túy",
}

SUBSTANCE_SYNONYMS = {
    "ma túy đá": "methamphetamine",
    "hàng đá": "methamphetamine",
    "thuốc lắc": "mdma",
    "ecstasy": "mdma",
    "ketamin": "ketamine",
    "heroin": "heroine",
}

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFC", name.strip().strip("\"'“”").lower())
    name = re.sub(r"^(?:tội\s+danh|tội)\s+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Standardize the two common Vietnamese spellings without losing readable canonical text.
    return name.replace("ma tuý", "ma túy")


def normalize_substance(name: str) -> str:
    """Normalize drug names and common journalistic aliases for entity resolution."""
    clean = normalize_crime(name)
    clean = re.sub(r"^(?:ma túy|chất ma túy|loại ma túy)\s+", "", clean).strip()
    return SUBSTANCE_SYNONYMS.get(clean, clean)


def link_entity(name: str, known: Collection[str],
                normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not isinstance(name, str) or not name.strip() or not known:
        return None

    clean_name = normalize(name)
    clean_to_original = {normalize(candidate): candidate for candidate in known if isinstance(candidate, str)}
    clean_to_original.pop("", None)
    if not clean_name or not clean_to_original:
        return None

    if clean_name in clean_to_original:
        return clean_to_original[clean_name]

    # Domain aliases are deliberately applied before fuzzy matching. An alias is only accepted
    # when its target exists in the supplied canonical collection, so this remains safe for reuse.
    alias_target = CRIME_SYNONYMS.get(clean_name)
    if alias_target and alias_target in clean_to_original:
        return clean_to_original[alias_target]

    matches = difflib.get_close_matches(clean_name, list(clean_to_original), n=1, cutoff=0.8)
    return clean_to_original[matches[0]] if matches else None

def find_substances(text: str) -> list[str]:
    normalized_text = normalize_crime(text)
    found = [name for name in SUBSTANCES if normalize_substance(name) in normalized_text]
    for alias, canonical in SUBSTANCE_SYNONYMS.items():
        if alias in normalized_text:
            match = link_entity(canonical, SUBSTANCES, normalize=normalize_substance)
            if match:
                found.append(match)
    return list(dict.fromkeys(found))

# ----------------------------------------------------------------------------------------------
# Extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    article_match = re.search(r"Điều\s+(\d+)", article_id, re.IGNORECASE)
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị\s+((?:phạt\s+tù|tù\s+chung\s+thân|tử\s+hình).+?)(?::|$)",
                            first_line, re.IGNORECASE)
        penalty_text = penalty.group(1).rstrip(".") if penalty else ""
        years = [int(value) for value in re.findall(r"(\d+)\s*năm", penalty_text)]
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty_text,
            "min_years": min(years) if years else None,
            "max_years": max(years) if years else None,
            "life_allowed": "chung thân" in penalty_text.lower(),
            "death_allowed": "tử hình" in penalty_text.lower(),
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "number": int(article_match.group(1)) if article_match else None,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài, không suy diễn. Trả về duy nhất một JSON object đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        try:
            raw = llm_fn(prompt, json_mode=True)
        except TypeError:  # Keep compatibility with simple injected callables.
            raw = llm_fn(prompt)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
        payload = json.loads(raw)
        cases = payload.get("cases", []) if isinstance(payload, dict) else []
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []
    if not isinstance(cases, list):
        return []
    for case in cases:
        if not isinstance(case, dict):
            continue
        raw_charges = case.get("charges", [])
        if not isinstance(raw_charges, list):
            raw_charges = []
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in raw_charges) if c})

        people = case.get("people", [])
        if not isinstance(people, list):
            case["people"] = []
            people = []
        for person in people:
            if isinstance(person, dict):
                person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""

        substances = case.get("substances", [])
        if not isinstance(substances, list):
            case["substances"] = []
            continue
        clean_substances = []
        for substance in substances:
            if not isinstance(substance, dict) or not substance.get("name"):
                continue
            canonical = link_entity(str(substance["name"]), SUBSTANCES, normalize=normalize_substance)
            if canonical:
                clean_substances.append({"name": canonical, "amount": str(substance.get("amount", ""))})
        case["substances"] = clean_substances
    return [case for case in cases if isinstance(case, dict)]


def _amount_grams(amount: str) -> float | None:
    """Best-effort conversion of the first kg/g quantity to grams; original text is always retained."""
    if not amount:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|kilôgam|kilogram|g|gam)\b", amount.lower())
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * 1000 if match.group(2) in {"kg", "kilôgam", "kilogram"} else value


def _threshold_grams(text: str, substance: str) -> tuple[float | None, float | None]:
    """Extract [minimum, exclusive maximum) from the clause line that names a substance."""
    line = next(
        (part for part in text.splitlines() if normalize_substance(substance) in normalize_crime(part)), ""
    )
    between = re.search(
        r"từ\s+(\d+(?:[.,]\d+)?)\s*(kilôgam|kilogram|kg|gam|g)\s+đến\s+dưới\s+"
        r"(\d+(?:[.,]\d+)?)\s*(kilôgam|kilogram|kg|gam|g)",
        line, re.IGNORECASE,
    )
    if between:
        low = _amount_grams(f"{between.group(1)} {between.group(2)}")
        high = _amount_grams(f"{between.group(3)} {between.group(4)}")
        return low, high
    at_least = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(kilôgam|kilogram|kg|gam|g)\s+trở\s+lên", line, re.IGNORECASE
    )
    if at_least:
        return _amount_grams(f"{at_least.group(1)} {at_least.group(2)}"), None
    return None, None


def _verdict_fields(sentence: str) -> dict[str, Any]:
    """Keep the source wording and expose numeric/boolean fields for Cypher filtering."""
    sentence = sentence.strip()
    years = re.search(r"(\d+)\s*năm", sentence, re.IGNORECASE)
    months = re.search(r"(\d+)\s*tháng", sentence, re.IGNORECASE)
    lowered = sentence.lower()
    return {
        "text": sentence,
        "years": int(years.group(1)) if years else None,
        "months": int(months.group(1)) if months else None,
        "is_life": "chung thân" in lowered,
        "is_death": "tử hình" in lowered,
    }

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- Optimized ontology writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("PenaltyRange", "id"),
                           ("Crime", "canonical_name"), ("Case", "id"), ("Substance", "name"),
                           ("Verdict", "id"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")
        self.run("CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE (p.name, p.doc_id) IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id})
              SET a.number = $number, a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {canonical_name: crime}) SET c.name = crime
                MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.clause_number = clause.number,
                  cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (_ IN CASE WHEN clause.penalty = '' THEN [] ELSE [1] END |
                MERGE (pr:PenaltyRange {id: clause.id})
                  SET pr.text = clause.penalty, pr.min_years = clause.min_years,
                      pr.max_years = clause.max_years, pr.life_allowed = clause.life_allowed,
                      pr.death_allowed = clause.death_allowed, pr.doc_id = $doc_id
                MERGE (cl)-[:DEFINES_PENALTY]->(pr))
            FOREACH (s IN clause.substances |
                MERGE (sub:Substance {name: s}) MERGE (cl)-[:THRESHOLDS_FOR]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        people = []
        case_charges = {charge for charge in case.get("charges", []) if charge}
        for raw_person in case.get("people", []):
            if not isinstance(raw_person, dict) or not str(raw_person.get("name", "")).strip():
                continue
            charge = str(raw_person.get("charge", "")).strip()
            if not charge and len(case_charges) == 1:
                charge = next(iter(case_charges))
            if charge:
                case_charges.add(charge)
            aliases = raw_person.get("aliases", [])
            if not isinstance(aliases, list):
                aliases = []
            sentence = str(raw_person.get("sentence", "")).strip()
            people.append({
                "name": str(raw_person["name"]).strip(),
                "aliases": [str(alias).strip() for alias in aliases if str(alias).strip()],
                "role": str(raw_person.get("role", "")).strip(),
                "charge": charge,
                "sentence": sentence,
                "verdict": _verdict_fields(sentence),
            })

        case_id = str(case.get("id") or f"{doc.id}#case")
        substances = [
            {"name": str(item["name"]), "amount": str(item.get("amount", "")),
             "amount_grams": _amount_grams(str(item.get("amount", "")))}
            for item in case.get("substances", [])
            if isinstance(item, dict) and item.get("name")
        ]
        self.run(
            """
            MERGE (k:Case {id: $case_id})
              SET k.name = $name, k.summary = $summary, k.date = $date,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:OCCURRED_IN]->(l))
            FOREACH (crime IN $charges |
                MERGE (c:Crime {canonical_name: crime}) SET c.name = crime
                MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances |
                MERGE (sub:Substance {name: s.name})
                MERGE (k)-[r:SEIZED]->(sub)
                SET r.amount = s.amount, r.amount_grams = s.amount_grams)
            """,
            case_id=case_id, name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=sorted(case_charges), substances=substances,
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )
        if people:
            self.run(
                """
                MATCH (k:Case {id: $case_id})
                UNWIND $people AS p
                MERGE (person:Person {name: p.name, doc_id: $doc_id})
                  SET person.aliases = p.aliases
                MERGE (person)-[d:DEFENDANT_IN]->(k)
                  SET d.role = p.role
                FOREACH (crime IN CASE WHEN p.charge = '' THEN [] ELSE [p.charge] END |
                    MERGE (c:Crime {canonical_name: crime}) SET c.name = crime
                    MERGE (person)-[:CHARGED_WITH]->(c))
                FOREACH (_ IN CASE WHEN p.sentence = '' THEN [] ELSE [1] END |
                    MERGE (v:Verdict {id: $case_id + '|' + p.name})
                      SET v.text = p.verdict.text, v.years = p.verdict.years,
                          v.months = p.verdict.months, v.is_life = p.verdict.is_life,
                          v.is_death = p.verdict.is_death, v.doc_id = $doc_id
                    MERGE (person)-[:RECEIVED]->(v))
                """,
                case_id=case_id, people=people, doc_id=doc.id,
            )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Return compact, prioritized facts using doc-based retrieval plus entity-based fallback."""
        doc_ids = list(dict.fromkeys(doc_id for doc_id in doc_ids if doc_id))
        seed_ids, seed_facts = self.seed_facts(
            question, doc_ids, skip_labels=("Clause", "PenaltyRange"), limit=min(max_facts, 20)
        )
        person_seeds = self.run(
            """
            MATCH (p:Person)
            WHERE (size(p.name) >= 3 AND toLower($q) CONTAINS toLower(p.name))
               OR any(a IN coalesce(p.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(p) AS id
            """,
            q=question,
        )
        person_seed_ids = [row["id"] for row in person_seeds]
        case_seed_ids = person_seed_ids or seed_ids

        # Tier 1: vector-hit doc_ids. Tier 2: a Person alias/Substance/etc. mentioned in the question.
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE ($has_person_seed AND EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $case_seed_ids })
               OR (NOT $has_person_seed AND (
                   k.doc_id IN $doc_ids
                   OR elementId(k) IN $case_seed_ids
                   OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $case_seed_ids }
               ))
            WITH DISTINCT k,
                 (elementId(k) IN $case_seed_ids OR
                  EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $case_seed_ids }) AS entity_match
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary,
                   k.doc_id AS doc_id, entity_match
            ORDER BY entity_match DESC, k.name
            LIMIT 20
            """,
            doc_ids=doc_ids, case_seed_ids=case_seed_ids,
            has_person_seed=bool(person_seed_ids),
        )
        case_ids = [row["id"] for row in cases]
        facts: list[str] = []
        for row in cases:
            summary = f": {row['summary']}" if row.get("summary") else ""
            facts.append(f"Vụ việc '{row['name']}' (nguồn {row['doc_id']}){summary}")

        crimes: set[str] = set()
        case_substance_amounts: dict[str, float] = {}
        if case_ids:
            people = self.run(
                """
                MATCH (p:Person)-[d:DEFENDANT_IN]->(k:Case)
                WHERE elementId(k) IN $case_ids
                OPTIONAL MATCH (p)-[:CHARGED_WITH]->(c:Crime)
                OPTIONAL MATCH (p)-[:RECEIVED]->(v:Verdict)
                RETURN DISTINCT p.name AS person, p.aliases AS aliases, d.role AS role,
                       c.canonical_name AS crime, v.text AS verdict
                ORDER BY p.name
                """,
                case_ids=case_ids,
            )
            for row in people:
                if row.get("crime"):
                    crimes.add(row["crime"])
                parts = [f"vai trò: {row['role']}" if row.get("role") else ""]
                parts += [f"tội: {row['crime']}" if row.get("crime") else "",
                          f"án tuyên: {row['verdict']}" if row.get("verdict") else ""]
                aliases = ", ".join(row.get("aliases") or [])
                if aliases:
                    parts.append(f"biệt danh: {aliases}")
                facts.append(f"Người '{row['person']}': " + "; ".join(part for part in parts if part))

            case_details = self.run(
                """
                MATCH (k:Case) WHERE elementId(k) IN $case_ids
                OPTIONAL MATCH (k)-[:CHARGED_WITH]->(c:Crime)
                OPTIONAL MATCH (k)-[s:SEIZED]->(sub:Substance)
                RETURN k.name AS case_name, collect(DISTINCT c.canonical_name) AS crimes,
                       collect(DISTINCT {name: sub.name, amount: s.amount,
                                         amount_grams: s.amount_grams}) AS substances
                """,
                case_ids=case_ids,
            )
            for row in case_details:
                crimes.update(crime for crime in row.get("crimes", []) if crime)
                seized = [
                    f"{item.get('amount', '')} {item.get('name', '')}".strip()
                    for item in row.get("substances", [])
                    if item.get("name")
                ]
                if seized:
                    facts.append(f"Tang vật vụ '{row['case_name']}': " + "; ".join(seized))
                for item in row.get("substances", []):
                    if item.get("name") and item.get("amount_grams") is not None:
                        case_substance_amounts[item["name"]] = max(
                            float(item["amount_grams"]), case_substance_amounts.get(item["name"], 0.0)
                        )

        article_numbers = [int(value) for value in re.findall(r"[Đđ]iều\s+(\d+)", question)]
        asked_substances = find_substances(question)
        law_rows = self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            OPTIONAL MATCH (a)-[:DEFINES]->(c:Crime)
            OPTIONAL MATCH (cl)-[:THRESHOLDS_FOR]->(sub:Substance)
            WITH a, cl, c, collect(DISTINCT sub.name) AS substances
            WHERE c.canonical_name IN $crimes
               OR a.number IN $article_numbers
               OR (size($crimes) = 0 AND a.doc_id IN $doc_ids)
               OR elementId(c) IN $seed_ids
            RETURN a.id AS article_id, a.title AS title, a.doc_id AS doc_id,
                   c.canonical_name AS crime, cl.number AS number, cl.penalty AS penalty,
                   cl.text AS text, substances
            ORDER BY a.number, cl.number
            """,
            crimes=sorted(crimes), article_numbers=article_numbers,
            doc_ids=doc_ids, seed_ids=seed_ids,
        )

        grouped: dict[str, list[dict]] = {}
        for row in law_rows:
            grouped.setdefault(row["article_id"], []).append(row)
        lowered_question = question.lower()
        asks_maximum = any(term in lowered_question for term in ("tối đa", "cao nhất", "khung cao nhất"))
        asks_basic = any(term in lowered_question for term in ("cơ bản", "khoản 1"))
        question_amount = _amount_grams(question)
        question_tokens = {
            token for token in re.findall(r"\w+", normalize_crime(question))
            if len(token) >= 4 and token not in {"theo", "trong", "những", "được", "nhiêu", "điều", "khoản"}
        }

        for rows in grouped.values():
            selected: list[dict]
            penalty_rows = [row for row in rows if row.get("penalty")]
            substance_rows = [
                row for row in rows
                if set(row.get("substances") or []) & set(asked_substances)
            ]
            applicable_rows = []
            if asked_substances and substance_rows:
                for row in substance_rows:
                    for substance in set(row.get("substances") or []) & set(asked_substances):
                        amount = case_substance_amounts.get(substance, question_amount)
                        low, high = _threshold_grams(row["text"], substance)
                        if amount is not None and low is not None and amount >= low and (high is None or amount < high):
                            applicable_rows.append(row)
                            break
            if asks_maximum and penalty_rows:
                selected = [max(penalty_rows, key=lambda row: row["number"])]
            elif applicable_rows:
                selected = applicable_rows
            elif asked_substances and substance_rows:
                selected = substance_rows
            elif asks_basic and penalty_rows:
                selected = [next((row for row in penalty_rows if row["number"] == 1), penalty_rows[0])]
            elif rows[0].get("crime") and penalty_rows:
                selected = [next((row for row in penalty_rows if row["number"] == 1), penalty_rows[0])]
            else:
                # Definition-style law articles can have many numbered entries. Keep only the
                # clauses with strongest question-word overlap to avoid flooding the prompt.
                selected = sorted(
                    rows,
                    key=lambda row: len(question_tokens & set(re.findall(r"\w+", normalize_crime(row["text"])))),
                    reverse=True,
                )[:4]

            for row in selected:
                detail = row["text"] if (not row.get("penalty") or asked_substances) else row["penalty"]
                facts.append(f"[{row['article_id']} - {row['title']}] khoản {row['number']}: {detail}")

        # Keep structured facts first. Generic seed edges are useful only when a custom/partial
        # extraction does not fit the optimized schema.
        facts.extend(seed_facts)
        return list(dict.fromkeys(fact for fact in facts if fact))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.suggested_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)

    known_crimes = sorted({article["crime"] for article in articles if article.get("crime")})
    for doc in news_docs:
        cases = extract_news_cases(doc, llm_fn, known_crimes)
        for index, case in enumerate(cases, start=1):
            case["id"] = f"{doc.id}#case-{index}"
            graph.add_news_case(case, doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Bạn là trợ lý pháp lý. Chỉ trả lời từ hai nguồn dưới đây.

[1. DỮ KIỆN TỪ ĐỒ THỊ TRI THỨC — ưu tiên cho tội danh, điều luật, khoản và mức án]
{facts}

[2. ĐOẠN VĂN TRÍCH DẪN TỪ BÁO CHÍ VÀ VĂN BẢN]
{chunks}

CÂU HỎI: {question}

NGUYÊN TẮC BẮT BUỘC:
1. Khi hỏi căn cứ hoặc khung hình phạt, dùng đúng Điều/Khoản trong dữ kiện đồ thị.
2. Khi có dữ liệu, nêu theo thứ tự: người/vụ việc → tội danh → mức án hoặc tang vật → căn cứ pháp luật.
3. Không thêm thông tin ngoài hai nguồn. Nếu hai nguồn chưa đủ, nói rõ phần nào chưa đủ.

CÂU TRẢ LỜI:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk.get("metadata", {}).get("doc_id") for chunk in chunks))
        doc_ids = [doc_id for doc_id in doc_ids if doc_id]
        facts = self.graph.context(question, doc_ids)
        chunk_context = "\n\n".join(
            f"[{index} | doc_id={chunk.get('metadata', {}).get('doc_id', '')}] {chunk['content']}"
            for index, chunk in enumerate(chunks, start=1)
        ) or "(không có đoạn văn phù hợp)"
        graph_context = "\n".join(f"- {fact}" for fact in facts) or "(không có dữ kiện đồ thị phù hợp)"
        return self.llm_fn(GRAPH_PROMPT.format(
            facts=graph_context, chunks=chunk_context, question=question,
        ))
