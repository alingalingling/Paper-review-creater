"""Retrieve fresh, selectable passages for a follow-up, including uncited pages."""
from __future__ import annotations

import math
import re
from collections import Counter

from .core import check_evidence, digest, tokens


def _chunks(text, size=900, overlap=180):
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        quote = text[start:end].strip()
        if quote:
            yield quote
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def generation_evidence(corpus, topic, requirements="", expand_query=None, budget=36000):
    """Keep exact source passages; lexical matches rank context but never gate AI writing."""
    candidates, ranges = [], []
    for doc in corpus["documents"]:
        first = len(candidates)
        for page in doc["pages"]:
            for quote in _chunks(page["text"]):
                candidates.append({"source_id":doc["id"], "filename":doc["filename"],
                                   "page":page["number"], "quote":quote})
        ranges.append((first, len(candidates)))
    total = sum(len(c["quote"]) for c in candidates)
    details = {"strategy":"full_context", "available_characters":total,
               "provided_characters":total, "search_terms":[]}
    # Small corpora are read in full, including when the topic and papers use different languages.
    if total <= budget:
        return candidates, details
    terms = expand_query() if expand_query else []
    details.update(strategy="ranked_context", search_terms=terms)
    queries = [(tokens(topic), 3), (tokens(requirements), 1), (tokens(" ".join(terms)), 2)]
    token_sets = [tokens(c["quote"]) for c in candidates]
    frequencies = Counter(term for word_set in token_sets for term in word_set)
    scores = [sum(weight * sum(math.log(1 + len(candidates) / frequencies[t])
                              for t in query & word_set)
                  for query, weight in queries) for word_set in token_sets]
    contextual = scores[:]
    anchors = []
    for first, end in ranges:
        if first == end:
            continue
        best = sorted(range(first, end), key=lambda i:scores[i], reverse=True)[:3]
        anchors.append(best[0])
        for i in best:
            for neighbour in range(max(first, i - 1), min(end, i + 2)):
                contextual[neighbour] = max(contextual[neighbour], scores[i] * .7)
    # Reserve a passage from each paper before filling remaining space with strong matches and neighbours.
    order = sorted(anchors, key=lambda i:scores[i], reverse=True)
    order += sorted(range(len(candidates)), key=lambda i:contextual[i], reverse=True)
    selected, used, size = [], set(), 0
    for i in order:
        if i in used or size + len(candidates[i]["quote"]) > budget:
            continue
        used.add(i)
        selected.append(i)
        size += len(candidates[i]["quote"])
    details["provided_characters"] = size
    return [candidates[i] for i in sorted(selected)], details


def revision_evidence(run, instruction, selected_id=None, budget=36000):
    """Return exact passages with stable IDs; all documents participate in retrieval."""
    recent = [turn["content"] for turn in run.get("conversation", [])
              if turn.get("role") == "user"][-4:]
    queries = [(tokens(instruction), 4), (tokens(" ".join(recent)), 1),
               (tokens(run.get("topic", "")), .3)]
    candidates, seen = [], set()

    def add(source_id, page, quote, filename, existing=False):
        key = (source_id, page, quote)
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "source_id": source_id, "page": page, "quote": quote,
            "filename": filename, "_tokens": tokens(quote), "_existing": existing,
        })

    # Adjacent chunks retain surrounding definitions and cross-page explanations.
    ranges = []
    for doc in run["corpus"]["documents"]:
        first = len(candidates)
        for page in doc["pages"]:
            for quote in _chunks(page["text"]):
                add(doc["id"], page["number"], quote, doc["filename"])
        ranges.append((first, len(candidates)))
    for claim in run["claims"]:
        for evidence in claim["evidence"]:
            checked = check_evidence(run["corpus"], evidence)
            if checked["valid"]:
                add(checked["source_id"], checked["page"], checked["quote"],
                    checked["filename"], existing=True)
    frequencies = Counter(term for c in candidates for term in c["_tokens"])
    scores = [
        sum(weight * sum(math.log(1 + len(candidates) / frequencies[t])
                         for t in query & c["_tokens"])
            for query, weight in queries)
        for c in candidates
    ]
    # Keep context around the strongest fresh matches, not just isolated keywords.
    scores_with_context = scores[:]
    for first, end in ranges:
        anchors = sorted(range(first, end), key=lambda i: scores[i], reverse=True)[:4]
        for i in anchors:
            if scores[i] <= 0:
                continue
            for neighbour in range(max(first, i - 2), min(end, i + 3)):
                scores_with_context[neighbour] = max(
                    scores_with_context[neighbour], scores[i] * (.8 ** abs(neighbour - i)))
    selected = next((c for c in run["claims"] if c["id"] == selected_id), {})
    selected_pages = {(e.get("source_id"), e.get("page")) for e in selected.get("evidence", [])}
    order = sorted(range(len(candidates)), key=lambda i: (
        scores_with_context[i] + (1 if candidates[i]["_existing"] else 0)
        + (.5 if (candidates[i]["source_id"], candidates[i]["page"]) in selected_pages else 0)
    ), reverse=True)
    passages, size = [], 0
    for i in order:
        c = candidates[i]
        if size + len(c["quote"]) > budget:
            continue
        identity = digest(f'{c["source_id"]}:{c["page"]}:{c["quote"]}')[:20]
        passages.append({key: value for key, value in c.items() if not key.startswith("_")}
                        | {"evidence_id": "E" + identity})
        size += len(c["quote"])

    # A whole-corpus term index corrects stale "not present" statements in chat history.
    terms = sorted(set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", instruction + " " + " ".join(recent))), key=str.lower)
    matches = []
    for term in dict.fromkeys(t.lower() for t in terms):
        for doc in run["corpus"]["documents"]:
            pages = [p["number"] for p in doc["pages"] if term in p["text"].lower()]
            if pages:
                matches.append({"term": term, "source_id": doc["id"],
                                "filename": doc["filename"], "pages": pages})
    return passages, matches
