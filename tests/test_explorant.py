from datetime import date

import config
import explorant as ex


def test_sources_due_per_day():
    # Juny 2026: dia 1 = dilluns (1a setmana).
    assert ex.sources_due(date(2026, 6, 1)) == ["senders_feec", "escapadaambnens"]
    assert ex.sources_due(date(2026, 6, 8)) == ["dexcursio"]       # 2n dilluns
    # 3r dilluns (timeout) i dia 15 (activitats recomanades) coincideixen.
    assert ex.sources_due(date(2026, 6, 15)) == ["timeout", "escapadaambnens_activitats"]
    assert "escapadaambnens_activitats" in ex.sources_due(date(2026, 7, 15))  # qualsevol dia 15
    assert ex.sources_due(date(2026, 6, 9)) == ["elmonensespera"]  # dimarts
    assert ex.sources_due(date(2026, 6, 10)) == ["sortirambnens"]  # dimecres
    assert ex.sources_due(date(2026, 6, 18)) == ["surtdecasa"]     # dijous
    assert ex.sources_due(date(2026, 6, 19)) == ["totnens"]        # divendres
    assert ex.sources_due(date(2026, 6, 20)) == ["barcelona_nens"] # dissabte
    assert ex.sources_due(date(2026, 6, 21)) == []                 # diumenge


def test_every_source_has_registry_entry():
    for keys in (ex.sources_due(date(2026, 6, d)) for d in range(1, 29)):
        for k in keys:
            assert k in ex.SOURCES


def test_clean_summary_strips_wordpress_tail():
    s = "Una excursió fantàstica. L'entrada Tal ha aparegut primer a D'excursió."
    assert ex._clean_summary(s) == "Una excursió fantàstica."
    assert ex._clean_summary("Text … Read More ho diu") == "Text"


def test_fitxa_key_is_source_plus_url():
    f = ex.Fitxa("k", "Nom", "https://w", "Títol", "https://w/x", "resum", "https://img")
    assert f.key() == "k|https://w/x"


def test_build_payload_image_to_explorant_subreddit():
    f = ex.Fitxa("surtdecasa", "Surt de casa", "https://surtdecasa.cat",
                 "Fira del llibre", "https://surtdecasa.cat/x", "resum",
                 "https://img.jpg", where="Tortosa")
    p = ex.build_payload(f, "https://r2/x.jpg", "comentari")
    assert p["tipus"] == "imatge"
    assert p["subreddit"] == config.EXPLORANT_SUBREDDIT
    assert p["url"] == "https://r2/x.jpg"
    assert p["comment_markdown"] == "comentari"
    assert p["source"] == "explorant"
    assert "Tortosa" in p["title"]
    assert "markdown" not in p


def test_build_comment_has_source_link_in_italics():
    f = ex.Fitxa("k", "Surt de casa", "https://w", "T", "https://w/x", "resum cru", "i")
    c = ex.build_comment(f, use_llm=False)
    assert "*Font: [Surt de casa](https://w/x)*" in c
