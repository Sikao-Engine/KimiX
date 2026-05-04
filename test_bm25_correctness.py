"""Quick correctness test for BM25."""
import sys
sys.path.insert(0, 'src')

from kimix.tools.skill.searching.bm25 import InvertedIndex, NgramTokenizer, Searcher

# Simple corpus
docs = [
    "the quick brown fox",
    "the lazy dog sleeps",
    "the quick dog jumps",
    "a brown fox sleeps quickly",
    "lazy dogs are quick",
]

tokenizer = NgramTokenizer(n=3)
index = InvertedIndex()
for i, doc in enumerate(docs):
    index.add_document(i, tokenizer.tokenize(doc))
index.finalize()

searcher = Searcher(index, tokenizer=tokenizer)

# Test 1: basic search
results = searcher.search("quick fox", top_k=3)
print("Search 'quick fox':", results)
assert len(results) <= 3
assert all(isinstance(r, tuple) and len(r) == 2 for r in results)
assert all(isinstance(r[0], int) and isinstance(r[1], float) for r in results)

# Test 2: doc 0 and doc 3 should rank high (contain quick/fox)
doc_ids = [r[0] for r in results]
assert 0 in doc_ids or 3 in doc_ids, f"Expected doc 0 or 3 in top results, got {doc_ids}"

# Test 3: empty query
assert searcher.search("") == []

# Test 4: no match
assert searcher.search("zzz xyz") == []

# Test 5: fuzzy search (typo)
results_fuzzy = searcher.search("quikc", top_k=3)
print("Search 'quikc' (fuzzy):", results_fuzzy)
# Should still find something since 'quick' is in vocab

# Test 6: score_topk vs score consistency
scorer = searcher.scorer
scores_dict = scorer.score(tokenizer.tokenize("quick fox"))
topk = scorer.score_topk(tokenizer.tokenize("quick fox"), top_k=10)
print("score dict keys:", sorted(scores_dict.keys()))
print("score_topk:", topk)
# topk should be the highest scoring docs
for doc_id, score in topk:
    assert scores_dict[doc_id] == score, f"Mismatch for doc {doc_id}"

print("All correctness tests passed!")
