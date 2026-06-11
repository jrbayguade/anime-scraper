import json
import pathlib
from datetime import datetime, timezone

import bluesky_manga as bm

_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "samfaina_feed.json"


def load_feed():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["feed"]


def post_by_uri(feed, frag):
    return next(i["post"] for i in feed if frag in i["post"]["uri"])


NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


def test_selects_most_recent_non_repost_monthly_post():
    post = bm.select_monthly_post(load_feed(), NOW)
    assert post is not None
    assert post["uri"].endswith("/JUNY")


def test_ignores_reposts_even_if_newer_match():
    # La repost de JULIOL és un match més recent que JUNY però s'ha de descartar.
    post = bm.select_monthly_post(load_feed(), NOW)
    assert "REPOSTJULIOL" not in post["uri"]


def test_returns_none_when_no_monthly_post():
    feed = [i for i in load_feed() if i["post"]["uri"].endswith("/RANDOM")]
    assert bm.select_monthly_post(feed, NOW) is None


def test_respects_recency_window():
    # Molt al futur: tots els matches queden fora dels 35 dies.
    far = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert bm.select_monthly_post(load_feed(), far) is None


def test_extract_month_year_from_text():
    created = datetime(2026, 6, 3, tzinfo=timezone.utc)
    assert bm.extract_month_year("DEL MES DE JUNY!", created) == "juny 2026"


def test_extract_month_year_handles_accented_month():
    created = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert bm.extract_month_year("...DEL MES DE MARÇ!", created) == "març 2026"


def test_extract_month_year_falls_back_to_createdat():
    created = datetime(2026, 5, 4, tzinfo=timezone.utc)
    assert bm.extract_month_year("sense mes al text", created) == "maig 2026"


def test_month_key():
    assert bm.month_key("juny 2026") == "2026-06"
    assert bm.month_key("març 2026") == "2026-03"


def test_extract_image_url_returns_fullsize():
    post = post_by_uri(load_feed(), "/JUNY")
    assert bm.extract_image_url(post) == "https://cdn.bsky.app/fullsize/juny.jpg"


def test_extract_image_url_none_when_no_embed():
    post = post_by_uri(load_feed(), "/RANDOM")
    assert bm.extract_image_url(post) is None


def test_extract_post_uri():
    post = post_by_uri(load_feed(), "/JUNY")
    assert bm.extract_post_uri(post).endswith("/JUNY")
