from app.rag.chunker import chunk_text, content_hash


def test_short_text_is_one_chunk():
    assert chunk_text("Open Monday to Saturday, 9am to 6pm.") == [
        "Open Monday to Saturday, 9am to 6pm."
    ]


def test_long_text_splits_with_overlap():
    text = " ".join(f"Sentence number {i} about the service department." for i in range(120))
    chunks = chunk_text(text, target_chars=400, overlap_chars=80)
    assert len(chunks) > 3
    assert all(len(c) <= 500 for c in chunks)


def test_empty_and_whitespace():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_hash_is_stable_and_content_sensitive():
    assert content_hash("a") == content_hash("a")
    assert content_hash("a") != content_hash("b")
