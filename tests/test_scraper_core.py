"""
Testy regresyjne logiki scrapera (scraper_core).

Nie wymagają fastapi/supabase — importują wyłącznie scraper_core (zależny tylko
od `requests`), więc uruchamiają się szybko:  pytest tests/

Pokrycie:
- score_website: brak strony, pusta/whitespace strona, działa (mobilna/niemobilna),
  nie działa (błąd HTTP i wyjątek połączenia), reguły opinii/ocen, brakujące dane.
- text_search_page: brak ponawiania na 1. stronie, ponawianie next_page_token na
  INVALID_REQUEST (realna przyczyna błędu "fizjoterapeuta Wrocław"), OVER_QUERY_LIMIT.
"""
import scraper_core
from scraper_core import DEFAULT_WEIGHTS, score_website, text_search_page

W = DEFAULT_WEIGHTS


class FakeResp:
    def __init__(self, ok=True, text=""):
        self.ok = ok
        self.text = text


# ── score_website ────────────────────────────────────────────────────────────

def test_no_website_scores_brak_strony():
    score, status, breakdown = score_website(None, None, None, W)
    assert status == "brak"
    assert score == W["brak_strony"]
    assert breakdown["stan_strony"]["punkty"] == W["brak_strony"]


def test_whitespace_website_treated_as_brak():
    # Google Places bywa zwraca "website" jako samą spację — to nie jest domena.
    score, status, _ = score_website("   ", None, None, W)
    assert status == "brak"
    assert score == W["brak_strony"]


def test_working_mobile_website(monkeypatch):
    monkeypatch.setattr(
        scraper_core.requests, "get",
        lambda *a, **k: FakeResp(ok=True, text='<meta name="viewport" content="width=device-width">'),
    )
    score, status, breakdown = score_website("https://przyklad.pl", None, None, W)
    assert status == "dziala"
    assert score == W["strona_dziala"]  # 0 pkt, mobilna
    assert "niemobilna" not in breakdown


def test_working_non_mobile_website_gets_bonus(monkeypatch):
    monkeypatch.setattr(
        scraper_core.requests, "get",
        lambda *a, **k: FakeResp(ok=True, text="<html><body>brak viewport</body></html>"),
    )
    score, status, breakdown = score_website("przyklad.pl", None, None, W)  # bez schematu
    assert status == "dziala"
    assert breakdown["niemobilna"]["punkty"] == W["niemobilna_bonus"]
    assert score == W["strona_dziala"] + W["niemobilna_bonus"]


def test_broken_website_http_error(monkeypatch):
    monkeypatch.setattr(scraper_core.requests, "get", lambda *a, **k: FakeResp(ok=False))
    score, status, _ = score_website("https://przyklad.pl", None, None, W)
    assert status == "nie_dziala"
    assert score == W["strona_nie_dziala"]


def test_broken_website_connection_exception(monkeypatch):
    def boom(*a, **k):
        raise scraper_core.requests.exceptions.ConnectionError("nope")

    monkeypatch.setattr(scraper_core.requests, "get", boom)
    score, status, _ = score_website("https://przyklad.pl", None, None, W)
    assert status == "nie_dziala"
    assert score == W["strona_nie_dziala"]


def test_reviews_and_rating_rules_no_website():
    # 12 opinii -> próg >=1 (5 pkt); ocena 4.3 -> próg >=4.0 (10 pkt).
    score, _, breakdown = score_website(None, 4.3, 12, W)
    assert breakdown["opinie"]["punkty"] == 5
    assert breakdown["ocena"]["punkty"] == 10
    assert score == W["brak_strony"] + 5 + 10


def test_high_reviews_and_rating_rules_no_website():
    # 60 opinii -> próg >=50 (20 pkt); ocena 4.8 -> próg >=4.5 (15 pkt).
    score, _, breakdown = score_website(None, 4.8, 60, W)
    assert breakdown["opinie"]["punkty"] == 20
    assert breakdown["ocena"]["punkty"] == 15
    assert score == W["brak_strony"] + 20 + 15


def test_missing_rating_and_reviews_are_safe():
    # None / "" nie mogą wywalić scoringu; brak reguł = brak pozycji w breakdown.
    score, _, breakdown = score_website(None, "", "", W)
    assert "opinie" not in breakdown
    assert "ocena" not in breakdown
    assert score == W["brak_strony"]


# ── text_search_page (stronicowanie + ponawianie) ────────────────────────────

def _patch_text_search(monkeypatch, responses):
    """Podmienia text_search sekwencją odpowiedzi; zlicza wywołania."""
    calls = {"n": 0}

    def fake(query, api_key, page_token=None):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    monkeypatch.setattr(scraper_core, "text_search", fake)
    return calls


def test_first_page_not_retried_on_invalid_request(monkeypatch):
    # 1. strona: INVALID_REQUEST to prawdziwy błąd zapytania — bez ponawiania.
    calls = _patch_text_search(monkeypatch, [{"status": "INVALID_REQUEST"}])
    data = text_search_page("q", "k", None, sleep=lambda *_: None)
    assert data["status"] == "INVALID_REQUEST"
    assert calls["n"] == 1


def test_first_page_over_query_limit_retried_once(monkeypatch):
    calls = _patch_text_search(monkeypatch, [{"status": "OVER_QUERY_LIMIT"}, {"status": "OK", "results": []}])
    data = text_search_page("q", "k", None, sleep=lambda *_: None)
    assert data["status"] == "OK"
    assert calls["n"] == 2


def test_next_page_token_invalid_then_ok(monkeypatch):
    # Token jeszcze nieaktywny (INVALID_REQUEST) -> ponawiamy aż się aktywuje.
    calls = _patch_text_search(
        monkeypatch,
        [{"status": "INVALID_REQUEST"}, {"status": "INVALID_REQUEST"}, {"status": "OK", "results": [1]}],
    )
    slept = []
    data = text_search_page("q", "k", "TOKEN", sleep=lambda d: slept.append(d))
    assert data["status"] == "OK"
    assert calls["n"] == 3
    assert len(slept) == 3  # spał przed każdą próbą (dając tokenowi czas)


def test_next_page_token_gives_up_after_retries(monkeypatch):
    # Token nigdy się nie aktywuje -> zwracamy ostatnie INVALID_REQUEST (caller
    # zakończy stronicowanie bez błędu zadania).
    calls = _patch_text_search(monkeypatch, [{"status": "INVALID_REQUEST"}])
    data = text_search_page("q", "k", "TOKEN", sleep=lambda *_: None)
    assert data["status"] == "INVALID_REQUEST"
    assert calls["n"] == 5  # pięć prób (2,3,4,5,6 s)
