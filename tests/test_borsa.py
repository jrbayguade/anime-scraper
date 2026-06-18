from datetime import date

import config
import borsa


def _closes():
    """Tancaments sintètics: SPY +1%, XLK +2%, XLE -2% (última sessió 17/06)."""
    return {
        "SPY": [(date(2026, 6, 16), 100.0), (date(2026, 6, 17), 101.0)],
        "XLK": [(date(2026, 6, 16), 50.0), (date(2026, 6, 17), 51.0)],
        "XLE": [(date(2026, 6, 16), 50.0), (date(2026, 6, 17), 49.0)],
    }


def test_fmt_pct_catalan():
    assert borsa.fmt_pct(0.82) == "+0,82%"
    assert borsa.fmt_pct(-1.3) == "-1,30%"
    assert borsa.fmt_pct(0.0) == "+0,00%"


def test_compute_changes_session_and_pct():
    session, changes = borsa.compute_changes(_closes())
    assert session == date(2026, 6, 17)  # la marca SPY
    assert round(changes["SPY"], 2) == 1.0
    assert round(changes["XLK"], 2) == 2.0
    assert round(changes["XLE"], 2) == -2.0


def test_compute_changes_session_without_spy():
    closes = {k: v for k, v in _closes().items() if k != "SPY"}
    session, _ = borsa.compute_changes(closes)
    assert session == date(2026, 6, 17)  # la més recent disponible


def test_build_rows_sorted_and_labeled_without_spy():
    _, changes = borsa.compute_changes(_closes())
    rows = borsa.build_rows(changes)
    assert [r.ticker for r in rows] == ["XLK", "XLE"]  # de més a menys %
    assert rows[0].label == "Tecnologia"
    assert rows[-1].label == "Energia"
    assert all(r.ticker != "SPY" for r in rows)  # SPY no és un sector


def test_build_stock_rows_sorted_by_market_cap():
    changes = {"AAPL": 1.0, "MSFT": -0.5, "NVDA": 2.0}
    caps = {"AAPL": 3.0e12, "MSFT": 3.2e12, "NVDA": 2.8e12}
    stocks = borsa.build_stock_rows(changes, caps)
    assert [s.ticker for s in stocks] == ["MSFT", "AAPL", "NVDA"]  # per capitalització
    assert stocks[0].name == "Microsoft"
    assert stocks[1].pct == 1.0


def test_build_stock_rows_needs_both_pct_and_cap():
    # AAPL té cap però no %; MSFT té % però no cap → cap dels dos surt.
    stocks = borsa.build_stock_rows({"MSFT": 1.0}, {"AAPL": 3.0e12})
    assert stocks == []


def test_build_stock_rows_empty_when_no_caps():
    # Fail-soft: sense capitalitzacions, el treemap es desactiva.
    assert borsa.build_stock_rows({"AAPL": 1.0, "MSFT": -0.5}, {}) == []


def test_build_title_includes_date_and_spy():
    title = borsa.build_title(date(2026, 6, 17), 1.0)
    assert "17/06/2026" in title
    assert "+1,00%" in title


def test_deterministic_comment_mentions_best_and_worst():
    _, changes = borsa.compute_changes(_closes())
    rows = borsa.build_rows(changes)
    comment = borsa.build_comment(rows, changes["SPY"], use_llm=False)
    assert "Tecnologia" in comment   # millor
    assert "Energia" in comment      # pitjor
    assert "+2,00%" in comment


def test_build_payload_is_image_post_to_borsa_subreddit():
    _, changes = borsa.compute_changes(_closes())
    rows = borsa.build_rows(changes)
    payload = borsa.build_payload(
        date(2026, 6, 17), "https://r2.example/borsa/2026-06-17.png",
        rows, changes["SPY"], "comentari",
    )
    assert payload["tipus"] == "imatge"
    assert payload["url"].endswith("2026-06-17.png")
    assert payload["comment_markdown"] == "comentari"
    assert payload["subreddit"] == config.BORSA_SUBREDDIT
    assert payload["source"] == "borsa"
    assert "markdown" not in payload  # un post d'imatge no porta cos de text


def test_history_round_trip(tmp_path, monkeypatch):
    hist = tmp_path / "borsa_history.json"
    monkeypatch.setattr(config, "BORSA_HISTORY_FILE", hist)
    assert borsa.last_session() is None
    borsa.save_session(date(2026, 6, 17))
    assert borsa.last_session() == "2026-06-17"


def test_push_skips_when_session_already_published(tmp_path, monkeypatch):
    hist = tmp_path / "borsa_history.json"
    monkeypatch.setattr(config, "BORSA_HISTORY_FILE", hist)
    borsa.save_session(date(2026, 6, 17))  # ja publicada

    monkeypatch.setattr(borsa, "fetch_closes", lambda tickers: _closes())
    called = {"enqueue": False}

    def _fail_enqueue(payload):
        called["enqueue"] = True
        return "x"

    monkeypatch.setattr(borsa.queue_store, "enqueue", _fail_enqueue)
    monkeypatch.setattr(borsa.sys, "argv", ["borsa.py", "--push", "--quiet"])

    assert borsa.main() == 0
    assert called["enqueue"] is False  # no s'encua res si la sessió no és nova
